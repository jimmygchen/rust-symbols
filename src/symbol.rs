use std::path::Path;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SymbolKind {
    Struct,
    Enum,
    Trait,
    Fn,
    Type,
    Const,
    Static,
    Mod,
    Union,
}

impl SymbolKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Struct => "struct",
            Self::Enum => "enum",
            Self::Trait => "trait",
            Self::Fn => "fn",
            Self::Type => "type",
            Self::Const => "const",
            Self::Static => "static",
            Self::Mod => "mod",
            Self::Union => "union",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Symbol {
    /// Fully qualified display name (e.g., "BeaconChain::import_block")
    pub name: String,
    pub kind: SymbolKind,
    /// Relative path from workspace root
    pub path: String,
    pub line: usize,
    /// Signature (truncated to 120 chars)
    pub signature: String,
    /// Crate this symbol belongs to
    pub crate_name: String,
}

/// File-level metadata: doc comment summary for each .rs file.
#[derive(Debug, Clone)]
pub struct FileDoc {
    /// Relative path from workspace root
    pub path: String,
    /// First `//!` doc comment line (the file's purpose), truncated to 100 chars
    pub summary: String,
}

pub trait SymbolExtractor {
    fn extract(&self, crate_path: &Path, crate_name: &str, workspace_root: &Path) -> Vec<Symbol>;
    fn extract_file_docs(
        &self,
        crate_path: &Path,
        crate_name: &str,
        workspace_root: &Path,
    ) -> Vec<FileDoc>;
}
