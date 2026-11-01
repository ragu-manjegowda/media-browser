// SPDX-License-Identifier: GPL-3.0-or-later
use std::collections::BTreeSet;

pub type Id = u64;

#[derive(Default, Debug)]
pub struct Selection {
    pub marked: BTreeSet<Id>,
    pub anchor: Option<Id>,
    base: BTreeSet<Id>,
}

impl Selection {
    pub fn visual(&mut self, cursor: Id) {
        if self.anchor.is_some() {
            self.stop();
        } else {
            self.anchor = Some(cursor);
            self.base = self.marked.clone();
            self.marked.insert(cursor);
        }
    }

    pub fn moved(&mut self, cursor: Id, order: &[Id]) {
        let Some(anchor) = self.anchor else { return };
        let Some(a) = order.iter().position(|id| *id == anchor) else {
            self.stop();
            return;
        };
        let Some(b) = order.iter().position(|id| *id == cursor) else {
            return;
        };
        self.marked = self.base.clone();
        self.marked.extend(&order[a.min(b)..=a.max(b)]);
    }

    pub fn stop(&mut self) {
        self.anchor = None;
        self.base.clear();
    }

    pub fn clear(&mut self) {
        self.stop();
        self.marked.clear();
    }

    pub fn can_open(&self) -> bool {
        self.anchor.is_none() && self.marked.is_empty()
    }

    pub fn remove(&mut self, id: Id) {
        self.marked.remove(&id);
        self.base.remove(&id);
        if self.anchor == Some(id) {
            self.stop();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shrink_preserve_and_add_disjoint_ranges() {
        let order: Vec<_> = (0..10).collect();
        let mut s = Selection::default();
        s.visual(2);
        s.moved(5, &order);
        assert_eq!(s.marked, (2..=5).collect());
        s.moved(3, &order);
        assert_eq!(s.marked, [2, 3].into());
        s.stop();
        s.moved(7, &order);
        assert_eq!(s.marked, [2, 3].into());
        s.visual(7);
        s.moved(9, &order);
        assert_eq!(s.marked, [2, 3, 7, 8, 9].into());
        s.moved(8, &order);
        assert_eq!(s.marked, [2, 3, 7, 8].into());
        s.clear();
        assert!(s.can_open());
    }

    #[test]
    fn enter_blocked_after_escape_and_rapid_moves_until_clear() {
        let mut s = Selection::default();
        s.visual(0);
        assert!(!s.can_open());
        s.moved(1, &[0, 1, 2]);
        s.moved(2, &[0, 1, 2]);
        s.stop();
        assert_eq!(s.marked, [0, 1, 2].into());
        assert!(!s.can_open());
        s.clear();
        assert!(s.can_open());
    }
}
