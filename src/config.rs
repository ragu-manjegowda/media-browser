// SPDX-License-Identifier: GPL-3.0-or-later
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use std::{env, fs, path::PathBuf};

pub const SIZES: [u32; 6] = [64, 96, 128, 192, 256, 384];
pub const RENDERER_VERSION: &str = "pqiv 2.13.3-media-browser9";
pub const LIBDIR: &str = match option_env!("MEDIA_BROWSER_LIBDIR") {
    Some(path) => path,
    None => "/usr/lib/media-browser",
};
pub const DATADIR: &str = match option_env!("MEDIA_BROWSER_DATADIR") {
    Some(path) => path,
    None => "/usr/share/media-browser",
};

#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub thumbnail_size: u32,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            thumbnail_size: 256,
        }
    }
}

pub fn home() -> Result<PathBuf> {
    env::var_os("HOME")
        .map(PathBuf::from)
        .context("HOME is not set")
}

pub fn xdg(name: &str, fallback: &str) -> Result<PathBuf> {
    match env::var_os(name).map(PathBuf::from) {
        Some(path) if path.is_absolute() => Ok(path),
        _ => Ok(home()?.join(fallback)),
    }
}

pub fn cache_root() -> Result<PathBuf> {
    Ok(xdg("XDG_CACHE_HOME", ".cache")?.join("media-browser/thumbnails"))
}

pub fn load() -> Result<(Config, String)> {
    let defaults = PathBuf::from(DATADIR);
    let user = xdg("XDG_CONFIG_HOME", ".config")?.join("media-browser");
    let mut cfg: Config = toml::from_str(&fs::read_to_string(defaults.join("config.toml"))?)?;
    let overrides = user.join("config.toml");
    if overrides.exists() {
        cfg = toml::from_str(&fs::read_to_string(overrides)?)?;
    }
    if !SIZES.contains(&cfg.thumbnail_size) {
        bail!("unsupported thumbnail_size");
    }
    let mut keys = fs::read_to_string(defaults.join("pqivrc"))?;
    let overrides = user.join("pqivrc");
    if overrides.exists() {
        keys.push('\n');
        keys.push_str(&fs::read_to_string(overrides)?.replace("[keybindings]", ""));
    }
    Ok((cfg, keys))
}
