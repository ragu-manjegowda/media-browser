// SPDX-License-Identifier: GPL-3.0-or-later
use crate::{
    cache,
    catalog::{self, Catalog, Source},
    config::{self, SIZES},
    renderer::{ManagedChild, Renderer},
    selection::{Id, Selection},
    thumbnails::{Scheduler, plan},
    trash, viewers,
};
use anyhow::Result;
use serde_json::json;
use std::{
    collections::{BTreeSet, HashSet},
    fs::{self, File},
    io::Write,
    os::unix::ffi::OsStrExt,
    os::unix::{ffi::OsStringExt, net::UnixDatagram},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

struct Trace(Option<File>);
impl Trace {
    fn lazy(&mut self, value: impl FnOnce() -> serde_json::Value) {
        if self.0.is_some() {
            self.write(value());
        }
    }
    fn write(&mut self, value: serde_json::Value) {
        if let Some(file) = &mut self.0 {
            let _ = writeln!(file, "{value}");
            let _ = file.flush();
        }
    }
}

pub fn run(mut arguments: Vec<PathBuf>) -> Result<()> {
    let (cfg, keys) = config::load()?;
    crate::renderer::verify()?;
    let root = config::cache_root()?;
    fs::create_dir_all(root.join("posters"))?;
    if arguments.is_empty() {
        let home = config::home()?;
        let pictures = home.join("Pictures");
        arguments.push(if pictures.is_dir() { pictures } else { home });
    }
    let runtime = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    let directory = tempfile::Builder::new()
        .prefix("media-browser-")
        .tempdir_in(runtime)?;
    let config = directory.path().join("pqivrc");
    fs::write(&config, keys)?;
    let (clipboard_requests, clipboard_sender) = UnixDatagram::pair()?;
    clipboard_requests.set_nonblocking(true)?;
    let mut sources = catalog::discover(&arguments, &root)?;
    let mut catalog = Catalog::new(directory.path(), &sources)?;
    let mut trace = Trace(
        std::env::var_os("MEDIA_BROWSER_TRACE")
            .map(File::create)
            .transpose()?,
    );
    trace.lazy(|| json!({"type":"catalog", "session":directory.path(), "renderer": crate::renderer::executable(), "sources": sources.iter().map(|s| json!({"id":s.id,"path":s.path,"video":s.video})).collect::<Vec<_>>() }));
    let mut renderer = Renderer::launch(&catalog.directory, &config, cfg.thumbnail_size)?;
    let mut scheduler = Scheduler::new(root.clone());
    let mut selection = Selection::default();
    let mut drawn = BTreeSet::<Id>::new();
    let mut viewers: Vec<ManagedChild> = vec![];
    let mut pending: Option<Vec<Source>> = None;
    let mut previous_view = None;
    let mut forward = true;
    let mut cursor = sources.first().map(|s| s.id);
    let mut size = cfg.thumbnail_size;
    let mut visible: Vec<Id> = sources.iter().map(|source| source.id).collect();
    let mut visible_set: HashSet<Id> = visible.iter().copied().collect();
    let mut query = String::new();
    let mut search_revision = 0;
    let mut pending_view = false;
    let mut waiting_view = false;
    let mut open_after_view = false;
    let mut last_search_status = None;
    let stopping = Arc::new(AtomicBool::new(false));
    for signal in [
        signal_hook::consts::SIGTERM,
        signal_hook::consts::SIGINT,
        signal_hook::consts::SIGHUP,
    ] {
        signal_hook::flag::register(signal, stopping.clone())?;
    }
    let mut scan = Instant::now();
    let mut counters_at = Instant::now();
    while !stopping.load(Ordering::Relaxed) && renderer.child.0.try_wait()?.is_none() {
        let event = renderer.events.recv_timeout(Duration::from_millis(20));
        if let Ok(event) = event {
            if event.revision > search_revision {
                query = String::from_utf8(glib::base64_decode(&event.query)).unwrap_or_default();
                search_revision = event.revision;
                selection.stop();
                visible = crate::search::filter(&sources, &query);
                visible_set = visible.iter().copied().collect();
                if !cursor.is_some_and(|id| visible_set.contains(&id)) {
                    cursor = visible.first().copied();
                }
                pending_view = true;
                waiting_view = true;
                open_after_view = false;
                previous_view = None;
                scheduler.prepare(vec![]);
            }
            if pending_view && event.library_total == sources.len() {
                renderer.view(&visible, cursor, search_revision)?;
                pending_view = false;
            }
            if event.event == "view" && event.view_revision == search_revision {
                waiting_view = false;
            }
            let current_view = !waiting_view
                && event.revision == search_revision
                && event.view_revision == search_revision;
            // A post-trash Enter can arrive while the native removal commands
            // are still being applied. Resolve it only against the acknowledged
            // replacement cursor, never a transient rank between removals.
            if waiting_view
                && event.revision == search_revision
                && event.event == "open"
                && pending.is_none()
                && selection.can_open()
            {
                open_after_view = true;
            }
            let action = if current_view && event.event == "view" && open_after_view {
                open_after_view = false;
                "open"
            } else {
                event.event.as_str()
            };
            // Stable IDs survive removals; stale renderer ranks are never used
            // to resolve an original-file operation.
            if current_view && event.id >= 0 && visible_set.contains(&(event.id as Id)) {
                cursor = Some(event.id as Id);
                selection.moved(event.id as Id, &visible);
            }
            if current_view {
                match action {
                    "copy_image" | "copy_path" if pending.is_none() => {
                        if let Some(source) = sources
                            .iter()
                            .find(|s| event.id >= 0 && s.id == event.id as Id)
                        {
                            renderer.copy(
                                &source.path,
                                event.event == "copy_image",
                                source.video,
                            )?;
                            trace.write(json!({"type":"clipboard-request", "path":source.path, "image":event.event == "copy_image"}));
                        }
                    }
                    "visual"
                        if pending.is_none()
                            && event.id >= 0
                            && visible_set.contains(&(event.id as Id)) =>
                    {
                        if let Some(id) = cursor {
                            selection.visual(id);
                        }
                    }
                    "stop" => selection.stop(),
                    "clear" => selection.clear(),
                    "sizeup" | "sizedown" => {
                        let i = SIZES.iter().position(|s| *s == size).unwrap_or(4);
                        let i = if event.event == "sizeup" {
                            (i + 1).min(SIZES.len() - 1)
                        } else {
                            i.saturating_sub(1)
                        };
                        size = SIZES[i];
                        renderer.send(&format!("set_thumbnail_size({size},{size})"))?;
                    }
                    "open" if pending.is_none() && selection.can_open() => {
                        if let Some(source) = sources.iter().find(|s| {
                            event.id >= 0 && s.id == event.id as Id && visible_set.contains(&s.id)
                        }) {
                            match viewers::open(source, &sources, &config, &clipboard_sender) {
                                Ok(child) => {
                                    trace.write(
                                    json!({"type":"viewer", "path":source.path,"pid":child.0.id()}),
                                );
                                    viewers.push(child);
                                }
                                Err(error) => {
                                    eprintln!("open {}: {error:#}", source.path.display())
                                }
                            }
                        }
                    }
                    "trash" if pending.is_none() => {
                        let selected: Vec<_> = sources
                            .iter()
                            .filter(|s| {
                                if selection.marked.is_empty() {
                                    event.id >= 0
                                        && s.id == event.id as Id
                                        && visible_set.contains(&s.id)
                                } else {
                                    selection.marked.contains(&s.id)
                                }
                            })
                            .cloned()
                            .collect();
                        if !selected.is_empty() {
                            selection.stop();
                            let path = directory.path().join("confirm.bin");
                            write_paths(&path, selected.iter().map(|s| s.path.as_path()))?;
                            trace.write(json!({"type":"confirmation", "paths":selected.iter().map(|s| &s.path).collect::<Vec<_>>()}));
                            pending = Some(selected);
                            renderer.send(&format!("mb_confirm({})", path.display()))?;
                        }
                    }
                    "no" => {
                        pending = None;
                    }
                    "yes" => {
                        if let Some(selected) = pending.take() {
                            let (removed, errors) = trash::trash(
                                &selected.iter().map(|s| s.path.clone()).collect::<Vec<_>>(),
                            );
                            trace.write(json!({"type":"trash", "removed":removed,"errors":errors}));
                            prune(
                                &mut sources,
                                &mut renderer,
                                &mut selection,
                                &mut cursor,
                                &mut visible,
                                &removed,
                            )?;
                            visible_set = visible.iter().copied().collect();
                            pending_view = true;
                            waiting_view = true;
                            previous_view = None;
                            if !errors.is_empty() {
                                let path = directory.path().join("errors.bin");
                                fs::write(&path, errors.join("\0") + "\0")?;
                                renderer.send(&format!("mb_error({})", path.display()))?;
                            }
                        }
                    }
                    _ => (),
                }
            }
            paint(&mut renderer, &selection, &mut drawn)?;
            if current_view && event.total == visible.len() && event.library_total == sources.len()
            {
                let signature = (event.first, event.count, visible.len());
                if previous_view != Some(signature) {
                    if let Some((first, _, _)) = previous_view
                        && first != event.first
                    {
                        forward = event.first > first;
                    }
                    let wanted = plan(visible.len(), event.first, event.count, forward)
                        .into_iter()
                        .filter_map(|i| {
                            sources
                                .iter()
                                .find(|source| source.id == visible[i])
                                .cloned()
                        })
                        .collect();
                    scheduler.prepare(wanted);
                    previous_view = Some(signature);
                }
            }
            let status = (
                selection.marked.len(),
                selection
                    .marked
                    .iter()
                    .filter(|id| !visible_set.contains(id))
                    .count(),
                visible.len(),
            );
            if last_search_status != Some(status) {
                renderer.send(&format!("mb_search_status({};{})", status.0, status.1))?;
                last_search_status = Some(status);
            }
            trace.lazy(|| json!({"type":"event", "native":event, "cursor":cursor, "marked":selection.marked,"anchor":selection.anchor, "query":query, "visible":visible, "work":scheduler.counters()}));
        }
        // Photo viewers report their actual browsed original, not the gallery
        // cursor. Only registered originals are forwarded to the gallery's
        // clipboard owner, which survives closing an individual photo viewer.
        let mut request = [0u8; 65536];
        while let Ok(length) = clipboard_requests.recv(&mut request) {
            if length < 2 || !matches!(request[0], b'I' | b'P') {
                continue;
            }
            let path = PathBuf::from(std::ffi::OsString::from_vec(request[1..length].to_vec()));
            if sources
                .iter()
                .any(|source| !source.video && source.path == path)
            {
                renderer.copy(&path, request[0] == b'I', false)?;
                trace.write(
                    json!({"type":"clipboard-request", "path":path, "image":request[0] == b'I'}),
                );
            }
        }
        for (source, fingerprint, outcome) in scheduler.poll() {
            if sources.iter().any(|s| s.id == source.id) {
                match catalog.publish(
                    &source,
                    &cache::poster_path(&root, &source.path),
                    &fingerprint,
                ) {
                    Ok(true) => {
                        renderer.send(&format!("reload_thumbnail({})", source.id))?;
                        trace.write(json!({"type":"poster", "id":source.id,"outcome":outcome}));
                    }
                    Ok(false) => (),
                    Err(error) => eprintln!("publish {}: {error:#}", source.path.display()),
                }
            }
        }
        if scan.elapsed() >= Duration::from_secs(2) {
            // Stat only the active window, never the entire library periodically.
            let wanted = scheduler.counters()["wanted"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            let missing: Vec<_> = sources
                .iter()
                .filter(|s| wanted.contains(&json!(s.id)) && !s.path.exists())
                .map(|s| s.path.clone())
                .collect();
            if !missing.is_empty() {
                prune(
                    &mut sources,
                    &mut renderer,
                    &mut selection,
                    &mut cursor,
                    &mut visible,
                    &missing,
                )?;
                visible_set = visible.iter().copied().collect();
                pending_view = true;
                waiting_view = true;
                previous_view = None;
            }
            scan = Instant::now();
        }
        let mut i = 0;
        while i < viewers.len() {
            if viewers[i].0.try_wait()?.is_some() {
                viewers.remove(i);
            } else {
                i += 1;
            }
        }
        if counters_at.elapsed() >= Duration::from_millis(100) {
            trace.write(json!({"type":"work", "work":scheduler.counters()}));
            counters_at = Instant::now();
        }
    }
    drop(scheduler);
    drop(viewers);
    trace.write(json!({"type":"exit"}));
    if !stopping.load(Ordering::Relaxed)
        && let Some(status) = renderer.child.0.try_wait()?
        && !status.success()
    {
        anyhow::bail!("renderer exited with {status}");
    }
    Ok(())
}

fn write_paths<'a>(path: &Path, paths: impl Iterator<Item = &'a Path>) -> Result<()> {
    let mut file = File::create(path)?;
    for path in paths {
        file.write_all(path.as_os_str().as_bytes())?;
        file.write_all(&[0])?;
    }
    file.flush()?;
    Ok(())
}

fn paint(renderer: &mut Renderer, selection: &Selection, drawn: &mut BTreeSet<Id>) -> Result<()> {
    for id in drawn.symmetric_difference(&selection.marked) {
        renderer.mark(*id, selection.marked.contains(id))?;
    }
    *drawn = selection.marked.clone();
    Ok(())
}

fn prune(
    sources: &mut Vec<Source>,
    renderer: &mut Renderer,
    selection: &mut Selection,
    cursor: &mut Option<Id>,
    visible: &mut Vec<Id>,
    removed: &[PathBuf],
) -> Result<()> {
    let previous = visible
        .iter()
        .position(|id| Some(*id) == *cursor)
        .unwrap_or(0);
    for (index, source) in sources.iter().enumerate().rev() {
        if removed.contains(&source.path) {
            renderer.send(&format!("remove_file_byindex({index})"))?;
            selection.remove(source.id);
        }
    }
    sources.retain(|s| !removed.contains(&s.path));
    visible.retain(|id| sources.iter().any(|source| source.id == *id));
    *cursor = visible
        .get(previous.min(visible.len().saturating_sub(1)))
        .copied();
    Ok(())
}
