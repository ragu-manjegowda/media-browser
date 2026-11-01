// SPDX-License-Identifier: GPL-3.0-or-later
use crate::{cache::Fingerprint, selection::Id};
use anyhow::Result;
use gdk_pixbuf::{Colorspace, Pixbuf};
use std::{
    cmp::Ordering,
    collections::{BTreeMap, HashSet},
    fs,
    io::{BufReader, Read, Write},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug)]
pub struct Source {
    pub id: Id,
    pub path: PathBuf,
    pub video: bool,
}

pub fn media_type(path: &Path) -> Option<bool> {
    let mut header = [0; 4096];
    let n = fs::File::open(path).ok()?.read(&mut header).ok()?;
    if gif_header(&header[..n]) {
        return Some(gif_is_animated(path));
    }
    let (kind, _) = gio::content_type_guess(Some(path), Some(&header[..n]));
    let mime = gio::content_type_get_mime_type(&kind)?;
    if mime.starts_with("video/") {
        Some(true)
    } else if mime.starts_with("image/") {
        Some(false)
    } else {
        None
    }
}

fn gif_header(header: &[u8]) -> bool {
    header.starts_with(b"GIF87a") || header.starts_with(b"GIF89a")
}

pub fn is_gif(path: &Path) -> bool {
    let mut header = [0; 6];
    fs::File::open(path)
        .and_then(|mut file| file.read_exact(&mut header))
        .is_ok()
        && gif_header(&header)
}

/// Inspect GIF block structure, never pixels. Stop at a second image descriptor;
/// compressed bytes are skipped, so embedded 0x2c bytes aren't mistaken for
/// frames. Loop metadata alone does not make a one-frame GIF an animation.
pub fn gif_is_animated(path: &Path) -> bool {
    fn byte(reader: &mut impl Read) -> std::io::Result<u8> {
        let mut value = [0];
        reader.read_exact(&mut value)?;
        Ok(value[0])
    }
    fn color_table(reader: &mut BufReader<fs::File>, packed: u8) -> std::io::Result<()> {
        if packed & 0x80 != 0 {
            reader.seek_relative(3 * (1_i64 << ((packed & 7) + 1)))?;
        }
        Ok(())
    }
    fn sub_blocks(reader: &mut BufReader<fs::File>) -> std::io::Result<()> {
        loop {
            let length = byte(reader)?;
            if length == 0 {
                return Ok(());
            }
            reader.seek_relative(i64::from(length))?;
        }
    }
    let parse = || -> std::io::Result<bool> {
        let mut reader = BufReader::new(fs::File::open(path)?);
        let mut header = [0; 13];
        reader.read_exact(&mut header)?;
        if !gif_header(&header) {
            return Ok(false);
        }
        color_table(&mut reader, header[10])?;
        let mut frames = 0;
        loop {
            match byte(&mut reader)? {
                0x21 => {
                    byte(&mut reader)?;
                    sub_blocks(&mut reader)?;
                }
                0x2c => {
                    let mut descriptor = [0; 9];
                    reader.read_exact(&mut descriptor)?;
                    frames += 1;
                    if frames == 2 {
                        return Ok(true);
                    }
                    color_table(&mut reader, descriptor[8])?;
                    byte(&mut reader)?; // LZW minimum code size
                    sub_blocks(&mut reader)?;
                }
                _ => return Ok(false), // trailer, truncation or invalid block
            }
        }
    };
    parse().unwrap_or(false)
}

#[cfg(test)]
pub(crate) fn gif_fixture(frames: usize) -> Vec<u8> {
    let mut data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\x00\x00".to_vec();
    for index in 0..frames {
        data.extend_from_slice(
            b"\x21\xf9\x04\x09\x0a\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02",
        );
        data.extend_from_slice(&[if index == 0 { 0x44 } else { 0x4c }, 0x01, 0]);
    }
    data.push(0x3b);
    data
}

fn natural_cmp(a: &Path, b: &Path) -> Ordering {
    let a = a.to_string_lossy().to_lowercase();
    let b = b.to_string_lossy().to_lowercase();
    let (a, b) = (a.as_bytes(), b.as_bytes());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        let order = if a[i].is_ascii_digit() && b[j].is_ascii_digit() {
            let (start_i, start_j) = (i, j);
            while i < a.len() && a[i].is_ascii_digit() {
                i += 1;
            }
            while j < b.len() && b[j].is_ascii_digit() {
                j += 1;
            }
            let aa = &a[start_i..i];
            let bb = &b[start_j..j];
            let aa = &aa[aa.iter().position(|c| *c != b'0').unwrap_or(aa.len())..];
            let bb = &bb[bb.iter().position(|c| *c != b'0').unwrap_or(bb.len())..];
            aa.len().cmp(&bb.len()).then_with(|| aa.cmp(bb))
        } else {
            let order = a[i].cmp(&b[j]);
            i += 1;
            j += 1;
            order
        };
        if order != Ordering::Equal {
            return order;
        }
    }
    a.len().cmp(&b.len())
}

pub fn discover(arguments: &[PathBuf], excluded: &Path) -> Result<Vec<Source>> {
    let mut pending = Vec::new();
    let mut files = BTreeMap::new();
    let mut visited = HashSet::new();
    for argument in arguments {
        let path = std::path::absolute(argument)?;
        if path.is_dir() {
            pending.push(path);
        } else {
            if let Some(video) = media_type(&path) {
                files.insert(path.clone(), video);
            }
            if let Some(parent) = path.parent() {
                for entry in fs::read_dir(parent)?.flatten() {
                    let p = entry.path();
                    if p.is_file()
                        && let Some(video) = media_type(&p)
                    {
                        files.insert(p, video);
                    }
                }
            }
        }
    }
    let excluded = fs::canonicalize(excluded).unwrap_or(excluded.to_path_buf());
    while let Some(path) = pending.pop() {
        let Ok(stat) = fs::metadata(&path) else {
            continue;
        };
        let real = fs::canonicalize(&path).unwrap_or(path.clone());
        if real.starts_with(&excluded) || !visited.insert((stat.dev(), stat.ino())) {
            continue;
        }
        let Ok(entries) = fs::read_dir(&path) else {
            continue;
        };
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                if entry.file_name() != ".sh_thumbnails" {
                    pending.push(p);
                }
            } else if p.is_file()
                && let Some(video) = media_type(&p)
            {
                files.insert(p, video);
            }
        }
    }
    let mut files: Vec<_> = files.into_iter().collect();
    files.sort_by(|(a, _), (b, _)| natural_cmp(a, b).then_with(|| a.cmp(b)));
    Ok(files
        .into_iter()
        .enumerate()
        .map(|(id, (path, video))| Source {
            id: id as Id,
            path,
            video,
        })
        .collect())
}

pub struct Catalog {
    pub directory: PathBuf,
    displayed: BTreeMap<Id, (Fingerprint, u128)>,
}

impl Catalog {
    pub fn new(directory: &Path, sources: &[Source]) -> Result<Self> {
        let entries = directory.join("entries");
        fs::create_dir(&entries)?;
        let placeholder = directory.join("loading.png");
        let image = Pixbuf::new(Colorspace::Rgb, true, 8, 512, 512).unwrap();
        image.fill(0);
        image.savev(&placeholder, "png", &[])?;
        for source in sources {
            fs::hard_link(&placeholder, entries.join(format!("{:012}.png", source.id)))?;
        }
        Ok(Self {
            directory: entries,
            displayed: BTreeMap::new(),
        })
    }

    pub fn publish(
        &mut self,
        source: &Source,
        poster: &Path,
        expected: &Fingerprint,
    ) -> Result<bool> {
        if Fingerprint::read(&source.path)? != *expected {
            return Ok(false);
        }
        let stamp = fs::metadata(poster)?
            .modified()?
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        if self.displayed.get(&source.id) == Some(&(expected.clone(), stamp)) {
            return Ok(false);
        }
        let mut temp = tempfile::NamedTempFile::new_in(&self.directory)?;
        let mut original = fs::File::open(poster)?;
        std::io::copy(&mut original, &mut temp)?;
        temp.flush()?;
        if Fingerprint::read(&source.path)? != *expected {
            return Ok(false);
        }
        temp.persist(self.directory.join(format!("{:012}.png", source.id)))?;
        self.displayed.insert(source.id, (expected.clone(), stamp));
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn discovery_natural_extensionless_cache_and_cycles() -> Result<()> {
        let d = tempfile::tempdir()?;
        let cache = d.path().join("cache");
        fs::create_dir(&cache)?;
        for name in [
            "image10.ppm",
            "image2.ppm",
            "photo one's",
            "cache/hidden.ppm",
        ] {
            fs::write(
                d.path().join(name),
                [b"P6\n1 1\n255\n".as_slice(), &[10, 100, 200]].concat(),
            )?;
        }
        std::os::unix::fs::symlink(d.path(), d.path().join("cycle"))?;
        let sources = discover(&[d.path().to_path_buf()], &cache)?;
        assert_eq!(
            sources
                .iter()
                .map(|s| s.path.file_name().unwrap().to_str().unwrap())
                .collect::<Vec<_>>(),
            ["image2.ppm", "image10.ppm", "photo one's"]
        );
        Ok(())
    }

    #[test]
    fn gif_animation_uses_frames_not_extension_or_loop_metadata() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let path = directory.path().join("no-extension");
        fs::write(&path, gif_fixture(2))?;
        assert!(gif_is_animated(&path));
        assert_eq!(media_type(&path), Some(true));
        let mut still = gif_fixture(1);
        still.splice(
            19..19,
            b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
                .iter()
                .copied(),
        );
        fs::write(&path, &still)?;
        assert!(
            !gif_is_animated(&path),
            "a loop tag does not create extra frames"
        );
        assert_eq!(media_type(&path), Some(false));
        still.pop();
        still.extend_from_slice(b"\x21\xfe\x0a\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x00\x3b");
        fs::write(&path, still)?;
        assert!(
            !gif_is_animated(&path),
            "descriptor bytes in sub-block data aren't frames"
        );
        fs::write(&path, b"GIF89a\x01")?;
        assert!(!gif_is_animated(&path));
        Ok(())
    }
}
