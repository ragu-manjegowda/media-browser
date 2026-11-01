// SPDX-License-Identifier: GPL-3.0-or-later
use gio::prelude::*;
use std::path::PathBuf;

/// Only confirmed original paths enter this function. GIO implements restore
/// metadata and per-filesystem trash. There is deliberately no unlink fallback.
pub fn trash(paths: &[PathBuf]) -> (Vec<PathBuf>, Vec<String>) {
    let mut removed = vec![];
    let mut errors = vec![];
    for path in paths {
        match gio::File::for_path(path).trash(gio::Cancellable::NONE) {
            Ok(()) => removed.push(path.clone()),
            Err(error) => errors.push(format!("{}: {error}", path.display())),
        }
    }
    (removed, errors)
}
