// SPDX-License-Identifier: GPL-3.0-or-later
use crate::{
    cache::{self, Fingerprint, Outcome},
    catalog::Source,
    selection::Id,
};
use anyhow::{Context, Result, bail};
use gdk_pixbuf::Pixbuf;
use std::{
    collections::{HashMap, HashSet, VecDeque},
    fs::File,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

/// Visible sources followed by exactly one adjacent page, never the intervening
/// entries after a far jump. The order also determines worker priority.
pub fn plan(len: usize, first: usize, count: usize, forward: bool) -> Vec<usize> {
    let first = first.min(len);
    let end = first.saturating_add(count).min(len);
    let before = first.saturating_sub(count)..first;
    let after = end..end.saturating_add(count).min(len);
    let adjacent = if forward {
        if after.is_empty() { before } else { after }
    } else if before.is_empty() {
        after
    } else {
        before
    };
    (first..end).chain(adjacent).collect()
}

fn run(mut command: Command, cancel: &AtomicBool, timeout: Duration) -> Result<File> {
    let output = tempfile::tempfile()?;
    command
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(output.try_clone()?);
    let mut process = crate::renderer::ManagedChild(command.spawn()?);
    let child = &mut process.0;
    let start = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            if !status.success() {
                bail!("decoder exited with {status}");
            }
            return Ok(output);
        }
        if cancel.load(Ordering::Relaxed) || start.elapsed() >= timeout {
            let _ = child.kill();
            let _ = child.wait();
            bail!("decoder cancelled or timed out");
        }
        thread::sleep(Duration::from_millis(20));
    }
}

fn representative_seek(path: &Path, cancel: &AtomicBool) -> f64 {
    use std::io::{Read, Seek};
    let mut probe = Command::new("/usr/bin/ffprobe");
    if crate::catalog::is_gif(path) {
        probe.args(["-ignore_loop", "1"]);
    }
    probe
        .args([
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
        ])
        .arg(path);
    run(probe, cancel, Duration::from_secs(5))
        .ok()
        .and_then(|mut file| {
            file.rewind().ok()?;
            let mut text = String::new();
            file.read_to_string(&mut text).ok()?;
            text.trim()
                .parse::<f64>()
                .ok()
                .filter(|v| v.is_finite())
                .map(|v| (v * 0.1).max(0.))
        })
        .unwrap_or(0.)
}

pub fn decode_frame(path: &Path, cancel: &AtomicBool) -> Result<Pixbuf> {
    use std::io::{Read, Seek};
    let seek = representative_seek(path, cancel);
    for timestamp in [seek, 0.] {
        if cancel.load(Ordering::Relaxed) {
            bail!("cancelled");
        }
        let mut command = Command::new("/usr/bin/ffmpeg");
        command
            .args([
                "-v",
                "error",
                "-nostdin",
                "-threads",
                "1",
                "-ss",
                &timestamp.to_string(),
                "-i",
            ])
            .arg(path)
            .args([
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-vf",
                "thumbnail=30,scale=512:512:force_original_aspect_ratio=decrease",
                "-filter_threads",
                "1",
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-f",
                "image2pipe",
                "-c:v",
                "png",
                "pipe:1",
            ]);
        if let Ok(mut file) = run(command, cancel, Duration::from_secs(15)) {
            file.rewind()?;
            let mut data = Vec::new();
            file.read_to_end(&mut data)?;
            use gdk_pixbuf::prelude::*;
            let loader = gdk_pixbuf::PixbufLoader::with_type("png")?;
            loader.write(&data)?;
            loader.close()?;
            return loader.pixbuf().context("decoded poster is empty");
        }
    }
    bail!("no representative frame in {}", path.display())
}

/// Decode a bounded animation sample, choosing informative visible content
/// instead of allowing an empty/transparent first frame to poison the cache.
pub fn decode_gif_frame(path: &Path, cancel: &AtomicBool) -> Result<Pixbuf> {
    let directory = tempfile::tempdir()?;
    let seek = representative_seek(path, cancel);
    let mut fallback: Option<(f64, Pixbuf)> = None;
    for timestamp in [0., seek * 5., seek * 9.] {
        let pattern = directory.path().join("frame-%02d.png");
        let mut command = Command::new("/usr/bin/ffmpeg");
        command
            .args([
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-threads",
                "1",
                "-ignore_loop",
                "1",
                "-ss",
                &timestamp.to_string(),
                "-i",
            ])
            .arg(path)
            .args([
                "-an",
                "-sn",
                "-vf",
                "scale=512:512:force_original_aspect_ratio=decrease,format=rgba",
                "-filter_threads",
                "1",
                "-frames:v",
                "32",
                "-fps_mode",
                "passthrough",
                "-threads",
                "1",
            ])
            .arg(&pattern);
        if run(command, cancel, Duration::from_secs(15)).is_err() {
            if cancel.load(Ordering::Relaxed) {
                bail!("cancelled");
            }
            continue;
        }
        let mut best: Option<(f64, Pixbuf)> = None;
        for index in 1..=32 {
            if cancel.load(Ordering::Relaxed) {
                bail!("cancelled");
            }
            let frame = directory.path().join(format!("frame-{index:02}.png"));
            if !frame.exists() {
                break;
            }
            let image = Pixbuf::from_file(&frame)?;
            let data = image.read_pixel_bytes();
            let channels = image.n_channels() as usize;
            let mut count = 0_u64;
            let (mut sum, mut squares, mut chroma) = (0_u64, 0_u64, 0_u64);
            for row in data
                .as_ref()
                .chunks(image.rowstride() as usize)
                .take(image.height() as usize)
            {
                for pixel in row[..image.width() as usize * channels].chunks(channels) {
                    if channels == 4 && pixel[3] < 16 {
                        continue;
                    }
                    let rgb = &pixel[..3];
                    let luma = rgb.iter().map(|v| u64::from(*v)).sum::<u64>() / 3;
                    count += 1;
                    sum += luma;
                    squares += luma * luma;
                    chroma += u64::from(rgb.iter().max().unwrap() - rgb.iter().min().unwrap());
                }
            }
            if count == 0 {
                continue;
            }
            let mean = sum as f64 / count as f64;
            let variance = (squares as f64 / count as f64 - mean * mean).max(0.);
            let coverage = count as f64 / f64::from(image.width() * image.height());
            let score = coverage * (1. + variance / 4096. + chroma as f64 / count as f64 / 64.);
            if best.as_ref().is_none_or(|(old, _)| score > *old) {
                best = Some((score, image));
            }
        }
        if let Some((score, image)) = best {
            if score > 1.01 {
                return Ok(image);
            }
            // An opaque, almost-uniform opening frame may also be a blank
            // lead-in. Try later samples, retaining a valid fallback for a
            // genuinely monochrome animation.
            if fallback.as_ref().is_none_or(|(old, _)| score > *old) {
                fallback = Some((score, image));
            }
        }
    }
    if let Some((_, image)) = fallback {
        return Ok(image);
    }
    bail!("no visible GIF frame in {}", path.display())
}

struct Job {
    source: Source,
    cancel: Arc<AtomicBool>,
    handle: JoinHandle<Result<(Fingerprint, Outcome)>>,
}

pub struct Scheduler {
    root: PathBuf,
    wanted: Vec<Source>,
    queue: VecDeque<Source>,
    active: Vec<Job>,
    known: HashMap<Id, (Fingerprint, std::time::SystemTime)>,
    scan_at: Instant,
    pub completed: u64,
    pub decoded: u64,
    pub cancelled: u64,
}

impl Scheduler {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            wanted: vec![],
            queue: VecDeque::new(),
            active: vec![],
            known: HashMap::new(),
            scan_at: Instant::now(),
            completed: 0,
            decoded: 0,
            cancelled: 0,
        }
    }

    pub fn prepare(&mut self, sources: Vec<Source>) {
        let wanted: HashSet<_> = sources.iter().map(|s| s.id).collect();
        for job in &self.active {
            if !wanted.contains(&job.source.id) && !job.cancel.swap(true, Ordering::Relaxed) {
                self.cancelled += 1;
            }
        }
        self.wanted = sources;
        self.scan();
    }

    fn scan(&mut self) {
        let active: HashSet<_> = self.active.iter().map(|j| j.source.id).collect();
        self.queue = self
            .wanted
            .iter()
            .filter(|s| !active.contains(&s.id))
            .filter(|s| {
                let current = Fingerprint::read(&s.path).ok();
                let stamp = std::fs::metadata(cache::poster_path(&self.root, &s.path))
                    .and_then(|m| m.modified())
                    .ok();
                match (self.known.get(&s.id), current, stamp) {
                    (Some((known, old_stamp)), Some(current), Some(stamp)) => {
                        *known != current || *old_stamp != stamp
                    }
                    _ => true,
                }
            })
            .cloned()
            .collect();
        self.scan_at = Instant::now() + Duration::from_secs(2);
        self.start();
    }

    fn start(&mut self) {
        while self.active.len() < 2 {
            let Some(source) = self.queue.pop_front() else {
                break;
            };
            let root = self.root.clone();
            let input = source.clone();
            let cancel = Arc::new(AtomicBool::new(false));
            let token = cancel.clone();
            let handle =
                thread::spawn(move || cache::generate(&root, &input.path, input.video, &token));
            self.active.push(Job {
                source,
                cancel,
                handle,
            });
        }
    }

    pub fn poll(&mut self) -> Vec<(Source, Fingerprint, Outcome)> {
        let mut ready = vec![];
        let mut i = 0;
        while i < self.active.len() {
            if !self.active[i].handle.is_finished() {
                i += 1;
                continue;
            }
            let job = self.active.remove(i);
            let result = job.handle.join();
            if job.cancel.load(Ordering::Relaxed) {
                continue;
            }
            match result {
                Ok(Ok((fingerprint, outcome)))
                    if Fingerprint::read(&job.source.path).ok().as_ref() == Some(&fingerprint) =>
                {
                    if let Ok(stamp) =
                        std::fs::metadata(cache::poster_path(&self.root, &job.source.path))
                            .and_then(|m| m.modified())
                    {
                        self.known
                            .insert(job.source.id, (fingerprint.clone(), stamp));
                    }
                    self.completed += 1;
                    if matches!(outcome, Outcome::Decoded) {
                        self.decoded += 1;
                    }
                    ready.push((job.source, fingerprint, outcome));
                }
                Ok(Err(error)) => eprintln!("thumbnail {}: {error:#}", job.source.path.display()),
                Err(_) => eprintln!("thumbnail worker panicked: {}", job.source.path.display()),
                _ => (),
            }
        }
        if Instant::now() >= self.scan_at {
            self.scan();
        }
        self.start();
        ready
    }

    pub fn counters(&self) -> serde_json::Value {
        serde_json::json!({"active": self.active.len(), "queued": self.queue.len(), "wanted": self.wanted.iter().map(|s| s.id).collect::<Vec<_>>(), "completed": self.completed, "decoded": self.decoded, "cancelled": self.cancelled})
    }
}

impl Drop for Scheduler {
    fn drop(&mut self) {
        for job in &self.active {
            job.cancel.store(true, Ordering::Relaxed);
        }
        for job in self.active.drain(..) {
            let _ = job.handle.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_viewport_reprioritization_and_far_jump() {
        assert_eq!(plan(100, 0, 4, true), (0..8).collect::<Vec<_>>());
        assert_eq!(plan(100, 52, 4, true), (52..60).collect::<Vec<_>>());
        assert_eq!(
            plan(100, 40, 4, false),
            (40..44).chain(36..40).collect::<Vec<_>>()
        );
        assert_eq!(
            plan(100, 96, 4, true),
            (96..100).chain(92..96).collect::<Vec<_>>()
        );
        assert!(plan(501, 441, 60, true).len() <= 120);
    }

    #[test]
    fn h264_hevc10_vp9_decoded_posters_and_reuse() -> Result<()> {
        let d = tempfile::tempdir()?;
        for (name, options) in [
            ("h264.mp4", vec!["-c:v", "libx264", "-bf", "3"]),
            (
                "hevc.mp4",
                vec![
                    "-c:v",
                    "libx265",
                    "-pix_fmt",
                    "yuv420p10le",
                    "-x265-params",
                    "pools=1:frame-threads=1:log-level=error",
                ],
            ),
            (
                "vp9.webm",
                vec![
                    "-c:v",
                    "libvpx-vp9",
                    "-deadline",
                    "realtime",
                    "-cpu-used",
                    "8",
                ],
            ),
        ] {
            let path = d.path().join(name);
            assert!(
                Command::new("/usr/bin/ffmpeg")
                    .args([
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc2=size=160x96:rate=10",
                        "-t",
                        "1",
                        "-an"
                    ])
                    .args(options)
                    .args(["-threads", "1"])
                    .arg(&path)
                    .status()?
                    .success()
            );
            let cancel = AtomicBool::new(false);
            assert!(matches!(
                cache::generate(d.path(), &path, true, &cancel)?.1,
                Outcome::Decoded
            ));
            let p = Pixbuf::from_file(cache::poster_path(d.path(), &path))?;
            let bytes = p.read_pixel_bytes();
            let unique: HashSet<_> = bytes.as_ref().chunks(3).take(1000).collect();
            assert!(
                unique.len() > 20,
                "poster must contain decoded color content"
            );
            assert!(matches!(
                cache::generate(d.path(), &path, true, &cancel)?.1,
                Outcome::Reused
            ));
        }
        Ok(())
    }
}
