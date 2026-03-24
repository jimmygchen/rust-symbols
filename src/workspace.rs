use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct CrateInfo {
    pub name: String,
    /// Path relative to workspace root (e.g., "crates/my_crate/")
    pub relative_path: String,
    /// Absolute path
    pub absolute_path: PathBuf,
    /// Direct dependency names (from Cargo.toml)
    pub deps: Vec<String>,
}

/// Discover workspace members from a Cargo.toml workspace definition.
pub fn discover_crates(workspace_root: &Path) -> Vec<CrateInfo> {
    let cargo_toml_path = workspace_root.join("Cargo.toml");
    let content = match std::fs::read_to_string(&cargo_toml_path) {
        Ok(c) => c,
        Err(_) => return Vec::new(),
    };

    let table: toml::Table = match content.parse() {
        Ok(t) => t,
        Err(_) => return Vec::new(),
    };

    let members = extract_members(&table);
    let mut crates = Vec::new();

    for pattern in &members {
        let resolved = resolve_glob_pattern(workspace_root, pattern);
        for abs_path in resolved {
            if let Some(info) = load_crate_info(workspace_root, &abs_path) {
                crates.push(info);
            }
        }
    }

    crates.sort_by(|a, b| a.name.cmp(&b.name));
    crates
}

fn extract_members(table: &toml::Table) -> Vec<String> {
    let workspace = match table.get("workspace").and_then(|w| w.as_table()) {
        Some(w) => w,
        None => return Vec::new(),
    };

    match workspace.get("members").and_then(|m| m.as_array()) {
        Some(arr) => arr
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect(),
        None => Vec::new(),
    }
}

fn resolve_glob_pattern(workspace_root: &Path, pattern: &str) -> Vec<PathBuf> {
    if pattern.contains('*') {
        // Glob pattern - expand it
        let full_pattern = workspace_root.join(pattern).join("Cargo.toml");
        let pattern_str = full_pattern.to_string_lossy().to_string();
        match glob_paths(&pattern_str) {
            Some(paths) => paths
                .into_iter()
                .filter_map(|p| p.parent().map(|p| p.to_path_buf()))
                .collect(),
            None => Vec::new(),
        }
    } else {
        let path = workspace_root.join(pattern);
        if path.join("Cargo.toml").exists() {
            vec![path]
        } else {
            Vec::new()
        }
    }
}

fn glob_paths(pattern: &str) -> Option<Vec<PathBuf>> {
    // Simple glob expansion using walkdir + pattern matching
    // Pattern like "/path/to/workspace/beacon_node/*/Cargo.toml"
    let pattern_path = Path::new(pattern);

    // Find the first component with a wildcard
    let mut base = PathBuf::new();
    let mut remaining = Vec::new();
    let mut found_glob = false;

    for component in pattern_path.components() {
        let s = component.as_os_str().to_string_lossy();
        if !found_glob && !s.contains('*') {
            base.push(component);
        } else {
            found_glob = true;
            remaining.push(s.to_string());
        }
    }

    if !base.exists() {
        return None;
    }

    let mut results = Vec::new();

    // Walk the base directory to the expected depth
    let depth = remaining.len();
    for entry in walkdir::WalkDir::new(&base)
        .min_depth(depth)
        .max_depth(depth)
        .into_iter()
        .flatten()
    {
        let entry_path = entry.path();
        // Check if the final component matches (usually "Cargo.toml")
        if let Some(last) = remaining.last() {
            if !last.contains('*') {
                if let Some(fname) = entry_path.file_name() {
                    if fname.to_string_lossy() == *last {
                        results.push(entry_path.to_path_buf());
                    }
                }
            } else {
                results.push(entry_path.to_path_buf());
            }
        }
    }

    Some(results)
}

fn load_crate_info(workspace_root: &Path, crate_path: &Path) -> Option<CrateInfo> {
    let cargo_toml = crate_path.join("Cargo.toml");
    let content = std::fs::read_to_string(&cargo_toml).ok()?;
    let table: toml::Table = content.parse().ok()?;

    let name = table
        .get("package")
        .and_then(|p| p.as_table())
        .and_then(|p| p.get("name"))
        .and_then(|n| n.as_str())
        .map(String::from)?;

    let deps = extract_dep_names(&table);

    let relative_path = crate_path
        .strip_prefix(workspace_root)
        .ok()?
        .to_string_lossy()
        .to_string();

    // Ensure trailing slash for consistency
    let relative_path = if relative_path.ends_with('/') {
        relative_path
    } else {
        format!("{}/", relative_path)
    };

    Some(CrateInfo {
        name,
        relative_path,
        absolute_path: crate_path.to_path_buf(),
        deps,
    })
}

fn extract_dep_names(table: &toml::Table) -> Vec<String> {
    let mut deps = Vec::new();
    for key in &["dependencies", "dev-dependencies", "build-dependencies"] {
        if let Some(dep_table) = table.get(*key).and_then(|d| d.as_table()) {
            for dep_name in dep_table.keys() {
                if !deps.contains(dep_name) {
                    deps.push(dep_name.clone());
                }
            }
        }
    }
    deps.sort();
    deps
}
