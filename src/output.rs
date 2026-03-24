use crate::symbol::{FileDoc, Symbol};
use crate::workspace::CrateInfo;
use chrono::Utc;
use std::collections::BTreeMap;
use std::io::Write;
use std::path::Path;

pub fn generate_crates_idx(crates: &[CrateInfo], output_dir: &Path) -> std::io::Result<()> {
    let path = output_dir.join("crates.txt");
    let mut f = std::io::BufWriter::new(std::fs::File::create(path)?);

    let ts = Utc::now().format("%Y-%m-%dT%H:%M:%SZ");
    writeln!(f, "# rust-index crates | {} | {} crates", ts, crates.len())?;
    writeln!(f, "# crate|path|deps")?;

    let crate_names: Vec<&str> = crates.iter().map(|c| c.name.as_str()).collect();
    for c in crates {
        let internal_deps: Vec<&str> = c
            .deps
            .iter()
            .filter(|d| crate_names.contains(&d.as_str()))
            .map(|d| d.as_str())
            .collect();
        writeln!(
            f,
            "{}|{}|{}",
            c.name,
            c.relative_path,
            internal_deps.join(",")
        )?;
    }

    Ok(())
}

pub fn generate_symbols_idx(symbols: &[Symbol], output_dir: &Path) -> std::io::Result<()> {
    let ts = Utc::now().format("%Y-%m-%dT%H:%M:%SZ");

    // Split symbols into per-crate files under symbols/ directory
    let symbols_dir = output_dir.join("symbols");
    std::fs::create_dir_all(&symbols_dir)?;

    let mut by_crate: BTreeMap<&str, Vec<&Symbol>> = BTreeMap::new();
    for s in symbols {
        by_crate.entry(&s.crate_name).or_default().push(s);
    }

    for (crate_name, crate_symbols) in &by_crate {
        let path = symbols_dir.join(format!("{}.txt", crate_name));
        let mut f = std::io::BufWriter::new(std::fs::File::create(path)?);
        writeln!(
            f,
            "# rust-index symbols/{} | {} | {} symbols",
            crate_name,
            ts,
            crate_symbols.len()
        )?;
        writeln!(f, "# name|kind|path|signature")?;

        for s in crate_symbols {
            writeln!(
                f,
                "{}|{}|{}|{}",
                s.name,
                s.kind.as_str(),
                s.path,
                s.signature
            )?;
        }
    }

    // Also generate a combined symbols.txt for grep-based usage
    let path = output_dir.join("symbols.txt");
    let mut f = std::io::BufWriter::new(std::fs::File::create(path)?);
    writeln!(
        f,
        "# rust-index symbols | {} | {} symbols",
        ts,
        symbols.len()
    )?;
    writeln!(f, "# name|kind|path|signature")?;
    writeln!(
        f,
        "# NOTE: Use grep on this file, do NOT read it fully. Per-crate splits in symbols/"
    )?;

    for s in symbols {
        writeln!(
            f,
            "{}|{}|{}|{}",
            s.name,
            s.kind.as_str(),
            s.path,
            s.signature
        )?;
    }

    Ok(())
}

pub struct ModuleInfo {
    pub crate_name: String,
    pub module_path: String,
    pub file_path: String,
    pub pub_count: usize,
    pub kinds_summary: String,
    pub doc_summary: String,
}

pub fn generate_modules_idx(
    symbols: &[Symbol],
    file_docs: &[FileDoc],
    output_dir: &Path,
) -> std::io::Result<()> {
    let path = output_dir.join("modules.txt");
    let mut f = std::io::BufWriter::new(std::fs::File::create(path)?);

    let ts = Utc::now().format("%Y-%m-%dT%H:%M:%SZ");

    // Group symbols by (crate_name, file_path)
    let mut file_groups: BTreeMap<(String, String), Vec<&Symbol>> = BTreeMap::new();
    for s in symbols {
        file_groups
            .entry((s.crate_name.clone(), s.path.clone()))
            .or_default()
            .push(s);
    }

    // Build doc comment lookup: file_path -> summary
    let doc_map: BTreeMap<&str, &str> = file_docs
        .iter()
        .map(|d| (d.path.as_str(), d.summary.as_str()))
        .collect();

    let modules: Vec<ModuleInfo> = file_groups
        .iter()
        .map(|((crate_name, file_path), syms)| {
            let module_path = derive_module_path(file_path, crate_name);
            let pub_count = syms.len();
            let kinds_summary = summarize_kinds(syms);
            let doc_summary = doc_map.get(file_path.as_str()).unwrap_or(&"").to_string();
            ModuleInfo {
                crate_name: crate_name.clone(),
                module_path,
                file_path: file_path.clone(),
                pub_count,
                kinds_summary,
                doc_summary,
            }
        })
        .collect();

    writeln!(f, "# rust-index modules | {}", ts)?;
    writeln!(f, "# crate|module_path|file|pub_count|kinds|doc")?;

    for m in &modules {
        writeln!(
            f,
            "{}|{}|{}|{}|{}|{}",
            m.crate_name, m.module_path, m.file_path, m.pub_count, m.kinds_summary, m.doc_summary
        )?;
    }

    Ok(())
}

fn derive_module_path(file_path: &str, _crate_name: &str) -> String {
    // Convert file path to module path
    // e.g., "beacon_node/network/src/sync/manager.rs" -> "sync::manager"
    // e.g., "beacon_node/network/src/lib.rs" -> ""
    // e.g., "beacon_node/network/src/router/mod.rs" -> "router"

    // Find "src/" and take everything after it
    let after_src = match file_path.find("src/") {
        Some(idx) => &file_path[idx + 4..],
        None => return String::new(),
    };

    // Remove .rs extension
    let without_ext = after_src.trim_end_matches(".rs");

    // Remove trailing /mod or lib
    let module = without_ext
        .trim_end_matches("/mod")
        .trim_end_matches("mod")
        .trim_end_matches("lib");

    // Convert path separators to ::
    let module = module.trim_matches('/').replace('/', "::");

    module
}

fn summarize_kinds(syms: &[&Symbol]) -> String {
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for s in syms {
        *counts.entry(s.kind.as_str()).or_insert(0) += 1;
    }

    counts
        .iter()
        .map(|(kind, count)| format!("{}{}", count, kind))
        .collect::<Vec<_>>()
        .join(",")
}

pub fn print_stats(crates: &[CrateInfo], symbols: &[Symbol], elapsed: std::time::Duration) {
    let mut kind_counts: BTreeMap<&str, usize> = BTreeMap::new();
    for s in symbols {
        *kind_counts.entry(s.kind.as_str()).or_insert(0) += 1;
    }

    let file_count = symbols
        .iter()
        .map(|s| &s.path)
        .collect::<std::collections::HashSet<_>>()
        .len();

    println!("rust-index stats:");
    println!("  Crates:  {}", crates.len());
    println!("  Files:   {}", file_count);
    println!("  Symbols: {}", symbols.len());
    println!("  By kind:");
    for (kind, count) in &kind_counts {
        println!("    {:8} {}", kind, count);
    }
    println!("  Time:    {:.2}s", elapsed.as_secs_f64());
}
