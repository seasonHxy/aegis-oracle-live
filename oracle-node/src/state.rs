use anyhow::{ensure, Context, Result};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct State {
    pub identity: String,
    pub last_price: Option<String>,
    pub last_sequence: u64,
    /// Persist the signed transaction before broadcasting: restart can safely rebroadcast identical bytes.
    pub pending_raw: Option<String>,
    pub pending_hash: Option<String>,
}
pub struct Store {
    pub state: State,
    path: PathBuf,
    _lock: File,
}
impl Store {
    pub fn open(path: &Path, identity: &str) -> Result<Self> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        std::fs::create_dir_all(parent)?;
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(path.with_extension("lock"))?;
        lock.try_lock_exclusive()
            .context("another oracle process owns this state")?;
        let state: State = if path.exists() {
            serde_json::from_slice(&std::fs::read(path)?)?
        } else {
            State {
                identity: identity.into(),
                ..Default::default()
            }
        };
        ensure!(state.identity==identity,"state belongs to another mode/network/contract/feed/config; use a different --state path");
        ensure!(
            state.pending_raw.is_some() == state.pending_hash.is_some(),
            "incomplete transaction journal"
        );
        Ok(Self {
            state,
            path: path.into(),
            _lock: lock,
        })
    }
    pub fn save(&self) -> Result<()> {
        let temporary = self.path.with_extension("tmp");
        let mut opts = OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut f = opts.open(&temporary)?;
        f.write_all(&serde_json::to_vec_pretty(&self.state)?)?;
        f.sync_all()?;
        std::fs::rename(&temporary, &self.path)?;
        let parent = self
            .path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        File::open(parent)?.sync_all()?;
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn isolates_and_locks_state() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");
        let s = Store::open(&path, "dry:a").unwrap();
        s.save().unwrap();
        assert!(Store::open(&path, "dry:a").is_err());
        drop(s);
        assert!(Store::open(&path, "evm:a").is_err());
        assert!(Store::open(&path, "dry:a").is_ok());
    }
    #[test]
    fn rejects_corrupt_state() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");
        std::fs::write(&path, "{}").unwrap();
        assert!(Store::open(&path, "x").is_err());
    }
}
