// SPDX-License-Identifier: GPL-3.0-or-later
mod app;
mod cache;
mod catalog;
mod config;
mod renderer;
mod search;
mod selection;
mod thumbnails;
mod trash;
mod viewers;

fn main() {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    if arguments.first().is_some_and(|s| s == "--version") {
        println!("media-browser {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    if arguments
        .first()
        .is_some_and(|s| s == "--help" || s == "-h")
    {
        println!(
            "media-browser [FILE-OR-DIRECTORY ...]\n\nMixed photo/video gallery. Defaults to ~/Pictures, then HOME.\nUser overrides: $XDG_CONFIG_HOME/media-browser/{{config.toml,pqivrc}}.\nmpv follows its normal configuration."
        );
        return;
    }
    if let Err(error) = app::run(
        arguments
            .into_iter()
            .map(std::path::PathBuf::from)
            .collect(),
    ) {
        eprintln!("media-browser: {error:#}");
        std::process::exit(1);
    }
}
