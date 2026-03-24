mod output;
mod symbol;
mod syn_extractor;
mod workspace;

use clap::Parser;
use std::path::PathBuf;
use std::time::Instant;

#[derive(Parser)]
#[command(
    name = "rust-index",
    about = "Generate compact code index for LLM agents"
)]
struct Cli {
    /// Path to workspace root (directory containing Cargo.toml)
    #[arg(default_value = ".")]
    path: PathBuf,

    /// Output directory for index files
    #[arg(short, long, default_value = ".ai/index")]
    output: PathBuf,

    /// Print statistics after generation
    #[arg(long)]
    stats: bool,

    /// Exclude pub static items (metrics, lazy counters) to reduce index size
    #[arg(long)]
    skip_statics: bool,
}

fn main() {
    let cli = Cli::parse();

    let workspace_root = cli.path.canonicalize().unwrap_or_else(|_| {
        eprintln!("Error: cannot resolve path '{}'", cli.path.display());
        std::process::exit(1);
    });

    let output_dir = if cli.output.is_relative() {
        workspace_root.join(&cli.output)
    } else {
        cli.output.clone()
    };

    std::fs::create_dir_all(&output_dir).unwrap_or_else(|e| {
        eprintln!("Error: cannot create output directory: {}", e);
        std::process::exit(1);
    });

    let start = Instant::now();

    // Discover workspace crates
    let crates = workspace::discover_crates(&workspace_root);
    if crates.is_empty() {
        eprintln!(
            "Warning: no workspace members found in {}",
            workspace_root.display()
        );
    }

    // Extract symbols and file docs from all crates
    let extractor = syn_extractor::SynExtractor;
    let mut all_symbols = Vec::new();
    let mut all_file_docs = Vec::new();

    for crate_info in &crates {
        let symbols = symbol::SymbolExtractor::extract(
            &extractor,
            &crate_info.absolute_path,
            &crate_info.name,
            &workspace_root,
        );
        all_symbols.extend(symbols);

        let docs = symbol::SymbolExtractor::extract_file_docs(
            &extractor,
            &crate_info.absolute_path,
            &crate_info.name,
            &workspace_root,
        );
        all_file_docs.extend(docs);
    }

    // Filter out statics if requested (metrics are low-value for navigation)
    if cli.skip_statics {
        all_symbols.retain(|s| s.kind != symbol::SymbolKind::Static);
    }

    // Sort symbols alphabetically
    all_symbols.sort_by(|a, b| a.name.cmp(&b.name));

    let elapsed = start.elapsed();

    // Generate index files
    output::generate_crates_idx(&crates, &output_dir).unwrap_or_else(|e| {
        eprintln!("Error writing crates.idx: {}", e);
        std::process::exit(1);
    });

    output::generate_symbols_idx(&all_symbols, &output_dir).unwrap_or_else(|e| {
        eprintln!("Error writing symbols.idx: {}", e);
        std::process::exit(1);
    });

    output::generate_modules_idx(&all_symbols, &all_file_docs, &output_dir).unwrap_or_else(|e| {
        eprintln!("Error writing modules.txt: {}", e);
        std::process::exit(1);
    });

    if cli.stats {
        output::print_stats(&crates, &all_symbols, elapsed);
    } else {
        println!(
            "Generated index: {} crates, {} symbols in {:.2}s -> {}",
            crates.len(),
            all_symbols.len(),
            elapsed.as_secs_f64(),
            output_dir.display()
        );
    }
}
