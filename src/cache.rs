// SPDX-License-Identifier: GPL-3.0-or-later
use anyhow::{Context, Result, bail};
use gdk_pixbuf::{InterpType, Pixbuf, PixbufLoader, prelude::*};
use gio::prelude::FileExt;
use serde::Serialize;
use std::{
    fs,
    os::unix::{ffi::OsStrExt, fs::MetadataExt},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
};

pub const BADGE_VERSION: &str = "play-v1";
pub const GIF_POSTER_VERSION: &str = "visible-v2";
pub const GIF_BADGE_VERSION: &str = "dots-v1";

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct Fingerprint {
    pub device: u64,
    pub inode: u64,
    pub mtime_ns: i128,
    pub ctime_ns: i128,
    pub size: u64,
}

impl Fingerprint {
    pub fn read(path: &Path) -> Result<Self> {
        let m = fs::metadata(path)?;
        Ok(Self {
            device: m.dev(),
            inode: m.ino(),
            mtime_ns: m.mtime() as i128 * 1_000_000_000 + m.mtime_nsec() as i128,
            ctime_ns: m.ctime() as i128 * 1_000_000_000 + m.ctime_nsec() as i128,
            size: m.size(),
        })
    }

    fn metadata(&self, path: &Path, video: bool) -> Vec<(&'static str, String)> {
        vec![
            ("tEXt::Thumb::URI", uri(path)),
            (
                "tEXt::Thumb::MTime",
                (self.mtime_ns / 1_000_000_000).to_string(),
            ),
            ("tEXt::Thumb::MTimeNS", self.mtime_ns.to_string()),
            ("tEXt::Thumb::CTimeNS", self.ctime_ns.to_string()),
            ("tEXt::Thumb::Device", self.device.to_string()),
            ("tEXt::Thumb::Inode", self.inode.to_string()),
            ("tEXt::Thumb::Size", self.size.to_string()),
            ("tEXt::Software", "media-browser".into()),
            (
                "tEXt::Thumb::MediaBrowserVideoBadge",
                if video && !crate::catalog::is_gif(path) {
                    BADGE_VERSION
                } else {
                    ""
                }
                .into(),
            ),
            (
                "tEXt::Thumb::MediaBrowserGifBadge",
                if crate::catalog::is_gif(path) {
                    GIF_BADGE_VERSION
                } else {
                    ""
                }
                .into(),
            ),
            (
                "tEXt::Thumb::MediaBrowserGifPoster",
                if video && crate::catalog::is_gif(path) {
                    GIF_POSTER_VERSION
                } else {
                    ""
                }
                .into(),
            ),
        ]
    }

    fn matches(&self, image: &Pixbuf, path: &Path) -> bool {
        let stored_uri = image.option("tEXt::Thumb::URI");
        // Keep valid legacy ASCII/private-cache metadata reusable. New writes
        // use escaped ASCII URIs: PNG tEXt cannot safely carry raw Unicode.
        if stored_uri.as_deref() != Some(uri(path).as_str())
            && stored_uri.as_deref() != Some(legacy_uri(path).as_str())
        {
            return false;
        }
        self.metadata(path, false)
            .iter()
            .skip(1)
            .take(6)
            .all(|(k, v)| image.option(k).as_deref() == Some(v.as_str()))
    }
}

pub fn uri(path: &Path) -> String {
    gio::File::for_path(path).uri().to_string()
}

fn legacy_uri(path: &Path) -> String {
    format!("file://{}", path.to_string_lossy())
}

pub fn poster_path(root: &Path, path: &Path) -> PathBuf {
    // Preserve the private, unescaped file URI cache namespace.
    let mut bytes = b"file://".to_vec();
    bytes.extend_from_slice(path.as_os_str().as_bytes());
    root.join("posters")
        .join(format!("{:x}.png", md5::compute(bytes)))
}

fn badge(image: &Pixbuf, gif: bool) -> Result<Pixbuf> {
    let result = image.copy().context("copying video poster")?;
    let shortest = result.width().min(result.height());
    let size = ((shortest as f64 * 0.22).round() as i32).clamp(1, 80);
    let margin = 12.min(shortest / 20);
    let left = result.width() - size - margin;
    let top = result.height() - size - margin;
    let symbol = if gif {
        (0..12)
            .map(|index| {
                let angle = f64::from(index) * std::f64::consts::TAU / 12.;
                format!(
                    "<circle cx='{}' cy='{}' r='2.8' fill='white' fill-opacity='0.9'/>",
                    32. + 22. * angle.cos(),
                    32. + 22. * angle.sin()
                )
            })
            .collect::<String>()
    } else {
        "<path d='M25 17 L48 32 L25 47 Z' fill='white' fill-opacity='0.9'/>".into()
    };
    let svg = format!(
        "<svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' viewBox='0 0 64 64'><circle cx='32' cy='32' r='30' fill='black' fill-opacity='0.45'/>{symbol}</svg>"
    );
    let loader = PixbufLoader::with_type("svg")?;
    loader.write(svg.as_bytes())?;
    loader.close()?;
    loader.pixbuf().context("SVG badge loader")?.composite(
        &result,
        left,
        top,
        size,
        size,
        left as f64,
        top as f64,
        1.,
        1.,
        InterpType::Bilinear,
        255,
    );
    Ok(result)
}

pub fn video_badge(image: &Pixbuf) -> Result<Pixbuf> {
    badge(image, false)
}
pub fn gif_badge(image: &Pixbuf) -> Result<Pixbuf> {
    badge(image, true)
}

pub fn store(
    image: &Pixbuf,
    target: &Path,
    path: &Path,
    expected: &Fingerprint,
    video: bool,
    cancel: &AtomicBool,
) -> Result<()> {
    let parent = target.parent().unwrap();
    fs::create_dir_all(parent)?;
    let temp = tempfile::NamedTempFile::new_in(parent)?;
    let metadata = expected.metadata(path, video);
    let options: Vec<_> = metadata.iter().map(|(k, v)| (*k, v.as_str())).collect();
    image.savev(temp.path(), "png", &options)?;
    if cancel.load(Ordering::Relaxed) || Fingerprint::read(path)? != *expected {
        bail!("stale or cancelled poster");
    }
    temp.persist(target)?;
    Ok(())
}

#[derive(Debug, Serialize)]
pub enum Outcome {
    Reused,
    Upgraded,
    Decoded,
}

pub fn generate(
    root: &Path,
    path: &Path,
    video: bool,
    cancel: &AtomicBool,
) -> Result<(Fingerprint, Outcome)> {
    if cancel.load(Ordering::Relaxed) {
        bail!("cancelled");
    }
    let expected = Fingerprint::read(path)?;
    let target = poster_path(root, path);
    let gif = crate::catalog::is_gif(path);
    let animated_gif = video && gif;
    if let Ok(cached) = Pixbuf::from_file(&target)
        && expected.matches(&cached, path)
        // Old GIF masters may be entirely transparent. Regenerate them rather
        // than only adding a play badge to an unusable first-frame poster.
        && (!animated_gif || cached.option("tEXt::Thumb::MediaBrowserGifPoster").as_deref() == Some(GIF_POSTER_VERSION))
    {
        let badge = cached.option("tEXt::Thumb::MediaBrowserVideoBadge");
        if gif {
            let marker = cached.option("tEXt::Thumb::MediaBrowserGifBadge");
            if marker.as_deref() == Some(GIF_BADGE_VERSION) {
                return Ok((expected, Outcome::Reused));
            }
            if !animated_gif
                && marker.as_deref().unwrap_or("").is_empty()
                && badge.as_deref().unwrap_or("").is_empty()
            {
                store(
                    &gif_badge(&cached)?,
                    &target,
                    path,
                    &expected,
                    false,
                    cancel,
                )?;
                return Ok((expected, Outcome::Upgraded));
            }
        } else if !video || badge.as_deref() == Some(BADGE_VERSION) {
            return Ok((expected, Outcome::Reused));
        } else if badge.as_deref().unwrap_or("").is_empty() {
            store(
                &video_badge(&cached)?,
                &target,
                path,
                &expected,
                true,
                cancel,
            )?;
            return Ok((expected, Outcome::Upgraded));
        }
    }
    let image = if animated_gif {
        Some(crate::thumbnails::decode_gif_frame(path, cancel)?)
    } else if !video {
        Pixbuf::from_file_at_scale(path, 512, 512, true)
            .and_then(|p| {
                p.apply_embedded_orientation().ok_or_else(|| {
                    glib::Error::new(gdk_pixbuf::PixbufError::Failed, "orientation failed")
                })
            })
            .ok()
    } else {
        None
    };
    let image = match image {
        Some(image) => image,
        None => crate::thumbnails::decode_frame(path, cancel)?,
    };
    let image = if gif {
        gif_badge(&image)?
    } else if video {
        video_badge(&image)?
    } else {
        image
    };
    store(&image, &target, path, &expected, video, cancel)?;
    Ok((expected, Outcome::Decoded))
}

#[cfg(test)]
mod tests {
    use super::*;
    use gdk_pixbuf::Colorspace;

    #[test]
    fn gif_dots_preserve_frame_and_upgrade_static_cache_without_decode() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let path = directory.path().join("legacy.gif");
        fs::write(&path, b"GIF89a not a decodable image")?;
        let image = Pixbuf::new(Colorspace::Rgb, false, 8, 512, 288).unwrap();
        image.fill(0x406080ff);
        let dotted = gif_badge(&image)?;
        let bytes = dotted.read_pixel_bytes();
        let pixel = |x: usize, y: usize| {
            bytes[y * dotted.rowstride() as usize + x * dotted.n_channels() as usize]
        };
        assert_eq!((dotted.width(), dotted.height()), (512, 288));
        assert_eq!(pixel(20, 20), 64);
        assert!(
            (1..64).contains(&pixel(470, 244)),
            "GIF center must not contain the video play triangle"
        );
        assert!(
            (201..255).contains(&pixel(490, 244)),
            "dotted ring is visible and translucent"
        );
        let target = poster_path(directory.path(), &path);
        fs::create_dir_all(target.parent().unwrap())?;
        let metadata = Fingerprint::read(&path)?.metadata(&path, false);
        let options: Vec<_> = metadata
            .iter()
            .filter(|(key, _)| !key.contains("MediaBrowserGif"))
            .map(|(key, value)| (*key, value.as_str()))
            .collect();
        image.savev(&target, "png", &options)?;
        let cancel = AtomicBool::new(false);
        assert!(matches!(
            generate(directory.path(), &path, false, &cancel)?.1,
            Outcome::Upgraded
        ));
        let stamp = fs::metadata(&target)?.modified()?;
        assert!(matches!(
            generate(directory.path(), &path, false, &cancel)?.1,
            Outcome::Reused
        ));
        assert_eq!(fs::metadata(&target)?.modified()?, stamp);
        Ok(())
    }

    #[test]
    fn animated_gif_replaces_transparent_legacy_poster_and_reuses_visible_frame() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let path = directory.path().join("animation one's α.GIF");
        fs::write(&path, crate::catalog::gif_fixture(2))?;
        let target = poster_path(directory.path(), &path);
        let blank = Pixbuf::new(Colorspace::Rgb, true, 8, 512, 512).unwrap();
        blank.fill(0);
        let cancel = AtomicBool::new(false);
        store(
            &blank,
            &target,
            &path,
            &Fingerprint::read(&path)?,
            false,
            &cancel,
        )?;
        assert!(matches!(
            generate(directory.path(), &path, true, &cancel)?.1,
            Outcome::Decoded
        ));
        let poster = Pixbuf::from_file(&target)?;
        assert_eq!(
            poster
                .option("tEXt::Thumb::MediaBrowserGifPoster")
                .as_deref(),
            Some(GIF_POSTER_VERSION)
        );
        assert_eq!(
            poster
                .option("tEXt::Thumb::MediaBrowserVideoBadge")
                .as_deref(),
            Some("")
        );
        assert_eq!(
            poster
                .option("tEXt::Thumb::MediaBrowserGifBadge")
                .as_deref(),
            Some(GIF_BADGE_VERSION)
        );
        let bytes = poster.read_pixel_bytes();
        let offset = 10 * poster.rowstride() as usize + 10 * poster.n_channels() as usize;
        assert_eq!(&bytes[offset..offset + 4], &[255, 0, 0, 255]);
        let stamp = fs::metadata(&target)?.modified()?;
        assert!(matches!(
            generate(directory.path(), &path, true, &cancel)?.1,
            Outcome::Reused
        ));
        assert_eq!(fs::metadata(&target)?.modified()?, stamp);
        Ok(())
    }
    #[test]
    fn badge_translucency_and_legacy_upgrade_without_decoder() -> Result<()> {
        let d = tempfile::tempdir()?;
        let path = d.path().join("legacy.mp4");
        fs::write(&path, b"not a decodable video")?;
        let p = Pixbuf::new(Colorspace::Rgb, false, 8, 512, 288).unwrap();
        p.fill(0x406080ff);
        let result = video_badge(&p)?;
        let bytes = result.read_pixel_bytes();
        let pixel =
            |x, y| bytes[y * result.rowstride() as usize + x * result.n_channels() as usize];
        assert_eq!((result.width(), result.height()), (512, 288));
        assert_eq!(pixel(20, 20), 64);
        assert!((1..64).contains(&pixel(445, 244)));
        assert!((201..255).contains(&pixel(470, 244)));
        let cancel = AtomicBool::new(false);
        store(
            &p,
            &poster_path(d.path(), &path),
            &path,
            &Fingerprint::read(&path)?,
            false,
            &cancel,
        )?;
        assert!(matches!(
            generate(d.path(), &path, true, &cancel)?.1,
            Outcome::Upgraded
        ));
        let stamp = fs::metadata(poster_path(d.path(), &path))?.modified()?;
        assert!(matches!(
            generate(d.path(), &path, true, &cancel)?.1,
            Outcome::Reused
        ));
        assert_eq!(
            fs::metadata(poster_path(d.path(), &path))?.modified()?,
            stamp
        );
        Ok(())
    }

    #[test]
    fn cache_fingerprints_corruption_new_files_and_independent_publication() -> Result<()> {
        use crate::catalog::{Catalog, Source};
        let d = tempfile::tempdir()?;
        let path = d.path().join("photo one's.ppm");
        let bytes = [b"P6\n2 1\n255\n".as_slice(), &[10, 100, 200, 10, 100, 200]].concat();
        fs::write(&path, &bytes)?;
        let other = d.path().join("other.ppm");
        fs::write(&other, &bytes)?;
        let a = Source {
            id: 0,
            path: path.clone(),
            video: false,
        };
        let b = Source {
            id: 1,
            path: other.clone(),
            video: false,
        };
        let mut catalog = Catalog::new(d.path(), &[a.clone(), b])?;
        let untouched = fs::read(catalog.directory.join("000000000001.png"))?;
        let cancel = AtomicBool::new(false);
        let (f, result) = generate(d.path(), &path, false, &cancel)?;
        assert!(matches!(result, Outcome::Decoded));
        let poster = poster_path(d.path(), &path);
        let stamp = fs::metadata(&poster)?.modified()?;
        assert!(matches!(
            generate(d.path(), &path, false, &cancel)?.1,
            Outcome::Reused
        ));
        assert_eq!(fs::metadata(&poster)?.modified()?, stamp);
        assert!(catalog.publish(&a, &poster, &f)?);
        assert!(!catalog.publish(&a, &poster, &f)?);
        assert_eq!(
            fs::read(catalog.directory.join("000000000001.png"))?,
            untouched
        );
        let mtime = fs::metadata(&path)?.modified()?;
        fs::write(
            &path,
            [b"P6\n2 1\n255\n".as_slice(), &[200, 100, 10, 200, 100, 10]].concat(),
        )?;
        fs::File::options()
            .write(true)
            .open(&path)?
            .set_modified(mtime)?;
        assert_ne!(Fingerprint::read(&path)?, f);
        assert!(matches!(
            generate(d.path(), &path, false, &cancel)?.1,
            Outcome::Decoded
        ));
        fs::write(&poster, b"corrupt")?;
        assert!(matches!(
            generate(d.path(), &path, false, &cancel)?.1,
            Outcome::Decoded
        ));
        assert!(matches!(
            generate(d.path(), &other, false, &cancel)?.1,
            Outcome::Decoded
        ));
        assert_eq!(fs::read_dir(d.path().join("posters"))?.count(), 2);
        // A changed source cannot publish an earlier completion.
        assert!(!catalog.publish(&a, &poster, &f)?);
        Ok(())
    }
}
