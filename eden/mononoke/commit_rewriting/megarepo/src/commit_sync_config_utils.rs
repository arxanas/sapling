/*
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

use ascii::AsciiString;
use metaconfig_types::{DefaultSmallToLargeCommitSyncPathAction, SmallRepoCommitSyncConfig};
use mononoke_types::MPath;
use std::collections::HashMap;

pub struct SmallRepoCommitSyncConfigDiff {
    pub default_action_change: Option<(
        DefaultSmallToLargeCommitSyncPathAction,
        DefaultSmallToLargeCommitSyncPathAction,
    )>,
    pub bookmark_prefix_change: Option<(AsciiString, AsciiString)>,
    pub mapping_added: HashMap<MPath, MPath>,
    pub mapping_changed: HashMap<MPath, (MPath, MPath)>,
    pub mapping_removed: HashMap<MPath, MPath>,
}

pub fn diff_small_repo_commit_sync_configs(
    from: SmallRepoCommitSyncConfig,
    to: SmallRepoCommitSyncConfig,
) -> SmallRepoCommitSyncConfigDiff {
    let default_action_change = if from.default_action == to.default_action {
        None
    } else {
        Some((from.default_action, to.default_action))
    };


    let bookmark_prefix_change = if from.bookmark_prefix == to.bookmark_prefix {
        None
    } else {
        Some((from.bookmark_prefix, to.bookmark_prefix))
    };

    let mut mapping_added = HashMap::new();
    let mut mapping_changed = HashMap::new();
    let mut mapping_removed = HashMap::new();

    for (from_small, from_large) in &from.map {
        match to.map.get(from_small) {
            Some(to_large) if from_large != to_large => {
                mapping_changed.insert(from_small.clone(), (from_large.clone(), to_large.clone()));
            }
            Some(_to_large) => {
                // nothing has changed
            }
            None => {
                mapping_removed.insert(from_small.clone(), from_large.clone());
            }
        };
    }

    for (to_small, to_large) in &to.map {
        if !from.map.contains_key(to_small) {
            mapping_added.insert(to_small.clone(), to_large.clone());
        }
    }

    SmallRepoCommitSyncConfigDiff {
        default_action_change,
        bookmark_prefix_change,
        mapping_added,
        mapping_changed,
        mapping_removed,
    }
}
