// SPDX-License-Identifier: GPL-3.0-or-later
use crate::{catalog::Source, selection::Id};
use std::path::Path;

fn subsequence(query: &str, text: &str) -> bool {
    let mut remaining = text.chars();
    query
        .chars()
        .all(|wanted| remaining.by_ref().any(|value| value == wanted))
}

/// Filter the original registry in natural order. Never renumber stable IDs or
/// search unrelated absolute-path prefixes such as a user's home directory.
pub fn filter(sources: &[Source], query: &str) -> Vec<Id> {
    let query = query.to_lowercase();
    let words: Vec<_> = query.split_whitespace().collect();
    let mut root = sources
        .first()
        .and_then(|source| source.path.parent())
        .unwrap_or(Path::new("/"));
    while !sources.iter().all(|source| source.path.starts_with(root)) {
        root = root.parent().unwrap_or(Path::new("/"));
    }
    sources
        .iter()
        .filter(|source| {
            let text = source
                .path
                .strip_prefix(root)
                .unwrap_or(&source.path)
                .to_string_lossy()
                .to_lowercase();
            words.iter().all(|word| subsequence(word, &text))
        })
        .map(|source| source.id)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fuzzy_names_folders_unicode_and_ids() {
        let sources: Vec<_> = [
            (4, "/media/2020/Alpine Morning.jpg"),
            (19, "/media/2021/Evening Lake.gif"),
            (32, "/media/2020/Résumé Solar.png"),
        ]
        .into_iter()
        .map(|(id, path)| Source {
            id,
            path: path.into(),
            video: false,
        })
        .collect();
        assert_eq!(filter(&sources, ""), vec![4, 19, 32]);
        assert_eq!(filter(&sources, "alp mrn"), vec![4]);
        assert_eq!(filter(&sources, "2020"), vec![4, 32]);
        assert_eq!(filter(&sources, "RÉSUM"), vec![32]);
        assert!(filter(&sources, "media").is_empty());
        assert!(filter(&sources, "no such item").is_empty());
    }
}
