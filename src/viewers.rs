// SPDX-License-Identifier: GPL-3.0-or-later
use crate::{
    catalog::Source,
    renderer::{self, ManagedChild},
};
use anyhow::Result;
use std::{
    os::{fd::OwnedFd, unix::net::UnixDatagram},
    path::Path,
    process::{Command, Stdio},
};

pub fn open(
    source: &Source,
    sources: &[Source],
    config: &Path,
    clipboard: &UnixDatagram,
) -> Result<ManagedChild> {
    if source.video {
        // mpv owns all config discovery and window/input policy.
        return Ok(ManagedChild(
            Command::new("/usr/bin/mpv")
                .arg("--")
                .arg(&source.path)
                .env_remove("MEDIA_BROWSER_EVENTS")
                .stdin(Stdio::null())
                .spawn()?,
        ));
    }
    let siblings: Vec<_> = sources
        .iter()
        .filter(|s| !s.video && s.path.parent() == source.path.parent())
        .collect();
    let index = siblings.iter().position(|s| s.id == source.id).unwrap_or(0);
    let mut command = Command::new(renderer::executable());
    command.env("MEDIA_BROWSER_PHOTO_VIEWER", "1");
    command.env("MEDIA_BROWSER_SESSION", config.parent().unwrap());
    if std::env::var_os("MEDIA_BROWSER_TRACE").is_some() {
        command.env(
            "MEDIA_BROWSER_PHOTO_STATE",
            config.with_file_name("photo-state.json"),
        );
    }
    Ok(ManagedChild(
        command
            .args(["--name=media-browser-viewer", "--end-of-files-action=wrap"])
            .arg(format!("--action=goto_file_byindex({index})"))
            .arg("--")
            .args(siblings.iter().map(|s| &s.path))
            .env("PQIVRC_PATH", config)
            .env_remove("MEDIA_BROWSER_EVENTS")
            .stdout(Stdio::from(OwnedFd::from(clipboard.try_clone()?)))
            .stdin(Stdio::null())
            .spawn()?,
    ))
}
