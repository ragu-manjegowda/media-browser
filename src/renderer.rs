// SPDX-License-Identifier: GPL-3.0-or-later
use crate::{config, selection::Id};
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use std::{
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread::{self, JoinHandle},
};

#[derive(Debug, Deserialize, serde::Serialize)]
pub struct Event {
    pub event: String,
    pub id: i64,
    pub first: usize,
    pub count: usize,
    pub columns: usize,
    pub rows: usize,
    pub size: u32,
    pub total: usize,
    #[serde(default)]
    pub library_total: usize,
    #[serde(default)]
    pub revision: u64,
    #[serde(default)]
    pub view_revision: u64,
    #[serde(default)]
    pub query: String,
    #[serde(default)]
    pub origin_x: i32,
    #[serde(default)]
    pub origin_y: i32,
    #[serde(default)]
    pub viewport_width: usize,
    #[serde(default)]
    pub viewport_height: usize,
}

pub struct ManagedChild(pub Child);

impl Drop for ManagedChild {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
        }
        let _ = self.0.wait();
    }
}

pub fn executable() -> std::path::PathBuf {
    Path::new(config::LIBDIR).join("pqiv")
}

pub fn verify() -> Result<()> {
    let output = Command::new(executable())
        .arg("--version")
        .env("NO_AT_BRIDGE", "1")
        .output()
        .context("private pqiv renderer is not installed")?;
    if !output.status.success()
        || String::from_utf8_lossy(&output.stdout).trim() != config::RENDERER_VERSION
    {
        bail!(
            "private renderer version mismatch; expected {}",
            config::RENDERER_VERSION
        );
    }
    Ok(())
}

pub struct Renderer {
    pub child: ManagedChild,
    input: Option<ChildStdin>,
    pub events: Receiver<Event>,
    reader: Option<JoinHandle<()>>,
}

impl Renderer {
    pub fn launch(entries: &Path, config: &Path, size: u32) -> Result<Self> {
        let mut child = Command::new(executable())
            .args([
                "--name=media-browser-gallery",
                "--sort",
                "--browse",
                "--allow-empty-window",
                "--end-of-files-action=wrap",
                "--thumbnail-persistence=off",
                "--thumbnail-preload=0",
                "--watch-files=off",
                "--actions-from-stdin",
                "--action=goto_file_byindex(0); montage_mode_enter()",
            ])
            .arg(format!("--thumbnail-size={size}x{size}"))
            .arg("--")
            .arg(entries)
            .env("PQIVRC_PATH", config)
            .env("MEDIA_BROWSER_EVENTS", "1")
            .env("MEDIA_BROWSER_SESSION", config.parent().unwrap())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()?;
        let input = child.stdin.take();
        let output = child.stdout.take().unwrap();
        let (tx, events) = mpsc::channel();
        let reader = thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let Ok(line) = line else { break };
                if let Ok(event) = serde_json::from_str(&line)
                    && tx.send(event).is_err()
                {
                    break;
                }
            }
        });
        Ok(Self {
            child: ManagedChild(child),
            input,
            events,
            reader: Some(reader),
        })
    }

    pub fn send(&mut self, action: &str) -> Result<()> {
        let Some(input) = self.input.as_mut() else {
            return Ok(());
        };
        if let Err(error) = writeln!(input, "{action}").and_then(|()| input.flush()) {
            // q can close the renderer between polling its exit status and a
            // worker completion. The event loop checks the real exit status.
            if error.kind() == std::io::ErrorKind::BrokenPipe {
                self.input.take();
            } else {
                return Err(error.into());
            }
        }
        Ok(())
    }

    pub fn mark(&mut self, id: Id, marked: bool) -> Result<()> {
        self.send(&format!("mb_mark({id}:{})", u8::from(marked)))
    }

    pub fn view(&mut self, ids: &[Id], cursor: Option<Id>, revision: u64) -> Result<()> {
        let list = ids.iter().map(u64::to_string).collect::<Vec<_>>().join(",");
        let text = format!(
            "{revision};{};{list}",
            cursor.map_or_else(|| "-1".into(), |id| id.to_string())
        );
        self.send(&format!(
            "mb_view({})",
            glib::base64_encode(text.as_bytes())
        ))
    }

    pub fn copy(&mut self, path: &Path, image: bool, motion: bool) -> Result<()> {
        use std::os::unix::ffi::OsStrExt;
        let encoded = glib::base64_encode(path.as_os_str().as_bytes());
        self.send(&format!(
            "mb_copy({}:{encoded})",
            if image && motion {
                "motion"
            } else if image {
                "image"
            } else {
                "path"
            }
        ))
    }
}

impl Drop for Renderer {
    fn drop(&mut self) {
        self.input.take();
        let _ = self.child.0.kill();
        let _ = self.child.0.wait();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}
