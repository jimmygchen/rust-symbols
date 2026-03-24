use crate::symbol::{FileDoc, Symbol, SymbolExtractor, SymbolKind};
use std::path::Path;

pub struct SynExtractor;

impl SymbolExtractor for SynExtractor {
    fn extract(&self, crate_path: &Path, crate_name: &str, workspace_root: &Path) -> Vec<Symbol> {
        let mut symbols = Vec::new();
        for (content, relative) in walk_rs_files(crate_path, workspace_root) {
            extract_from_source(&content, &relative, crate_name, &mut symbols);
        }
        symbols
    }

    fn extract_file_docs(
        &self,
        crate_path: &Path,
        _crate_name: &str,
        workspace_root: &Path,
    ) -> Vec<FileDoc> {
        let mut docs = Vec::new();
        for (content, relative) in walk_rs_files(crate_path, workspace_root) {
            if let Some(summary) = extract_file_doc_comment(&content) {
                docs.push(FileDoc {
                    path: relative,
                    summary,
                });
            }
        }
        docs
    }
}

fn walk_rs_files(crate_path: &Path, workspace_root: &Path) -> Vec<(String, String)> {
    let src_dir = crate_path.join("src");
    if !src_dir.exists() {
        return Vec::new();
    }

    let mut files = Vec::new();
    let walker = ignore::WalkBuilder::new(&src_dir)
        .hidden(false)
        .git_ignore(true)
        .build();

    for entry in walker.into_iter().flatten() {
        let path = entry.path();
        if path.extension().is_some_and(|e| e == "rs") {
            if let Ok(content) = std::fs::read_to_string(path) {
                let relative = path
                    .strip_prefix(workspace_root)
                    .unwrap_or(path)
                    .to_string_lossy()
                    .to_string();
                files.push((content, relative));
            }
        }
    }

    files
}

/// Extract the first `//!` doc comment line from a file as a summary.
fn extract_file_doc_comment(content: &str) -> Option<String> {
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("//!") {
            let doc = trimmed.trim_start_matches("//!").trim();
            if !doc.is_empty() {
                let truncated = if doc.len() > 100 {
                    format!("{}...", &doc[..97])
                } else {
                    doc.to_string()
                };
                return Some(truncated);
            }
        } else if !trimmed.is_empty() && !trimmed.starts_with("//") && !trimmed.starts_with('#') {
            // Hit non-comment, non-attribute code — no file doc comment
            break;
        }
    }
    None
}

fn extract_from_source(
    content: &str,
    relative_path: &str,
    crate_name: &str,
    symbols: &mut Vec<Symbol>,
) {
    let file = match syn::parse_file(content) {
        Ok(f) => f,
        Err(_) => return,
    };

    for item in &file.items {
        extract_item(item, relative_path, crate_name, content, symbols);
    }
}

fn extract_item(
    item: &syn::Item,
    path: &str,
    crate_name: &str,
    source: &str,
    symbols: &mut Vec<Symbol>,
) {
    match item {
        syn::Item::Struct(s) if is_pub(&s.vis) => {
            let name = s.ident.to_string();
            let line = line_of_span(source, s.ident.span());
            let sig = extract_signature(source, line);
            symbols.push(Symbol {
                name: name.clone(),
                kind: SymbolKind::Struct,
                path: path.to_string(),
                line,
                signature: sig,
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Enum(e) if is_pub(&e.vis) => {
            let name = e.ident.to_string();
            let line = line_of_span(source, e.ident.span());
            let sig = extract_signature(source, line);
            symbols.push(Symbol {
                name: name.clone(),
                kind: SymbolKind::Enum,
                path: path.to_string(),
                line,
                signature: sig,
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Trait(t) if is_pub(&t.vis) => {
            let name = t.ident.to_string();
            let line = line_of_span(source, t.ident.span());
            let sig = extract_signature(source, line);
            symbols.push(Symbol {
                name,
                kind: SymbolKind::Trait,
                path: path.to_string(),
                line,
                signature: sig,
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Fn(f) if is_pub(&f.vis) => {
            let name = f.sig.ident.to_string();
            let line = line_of_span(source, f.sig.ident.span());
            let sig = extract_fn_signature(&f.sig, &f.vis);
            symbols.push(Symbol {
                name,
                kind: SymbolKind::Fn,
                path: path.to_string(),
                line,
                signature: sig,
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Type(t) if is_pub(&t.vis) => {
            let name = t.ident.to_string();
            let line = line_of_span(source, t.ident.span());
            let sig = extract_signature(source, line);
            symbols.push(Symbol {
                name,
                kind: SymbolKind::Type,
                path: path.to_string(),
                line,
                signature: sig,
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Const(c) if is_pub(&c.vis) => {
            let name = c.ident.to_string();
            let line = line_of_span(source, c.ident.span());
            let sig = extract_signature(source, line);
            symbols.push(Symbol {
                name,
                kind: SymbolKind::Const,
                path: path.to_string(),
                line,
                signature: sig,
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Static(s) if is_pub(&s.vis) => {
            let name = s.ident.to_string();
            let line = line_of_span(source, s.ident.span());
            let sig = extract_signature(source, line);
            symbols.push(Symbol {
                name,
                kind: SymbolKind::Static,
                path: path.to_string(),
                line,
                signature: sig,
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Mod(m) if is_pub(&m.vis) => {
            let name = m.ident.to_string();
            let line = line_of_span(source, m.ident.span());
            symbols.push(Symbol {
                name,
                kind: SymbolKind::Mod,
                path: path.to_string(),
                line,
                signature: format!("pub mod {}", m.ident),
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Union(u) if is_pub(&u.vis) => {
            let name = u.ident.to_string();
            let line = line_of_span(source, u.ident.span());
            let sig = extract_signature(source, line);
            symbols.push(Symbol {
                name,
                kind: SymbolKind::Union,
                path: path.to_string(),
                line,
                signature: sig,
                crate_name: crate_name.to_string(),
            });
        }
        syn::Item::Impl(imp) => {
            extract_impl(imp, path, crate_name, source, symbols);
        }
        _ => {}
    }
}

fn extract_impl(
    imp: &syn::ItemImpl,
    path: &str,
    crate_name: &str,
    source: &str,
    symbols: &mut Vec<Symbol>,
) {
    let type_name = match &*imp.self_ty {
        syn::Type::Path(tp) => {
            let segments: Vec<_> = tp
                .path
                .segments
                .iter()
                .map(|s| s.ident.to_string())
                .collect();
            segments.join("::")
        }
        _ => return,
    };

    for item in &imp.items {
        match item {
            syn::ImplItem::Fn(method) if is_pub_impl(&method.vis) => {
                let name = format!("{}::{}", type_name, method.sig.ident);
                let line = line_of_span(source, method.sig.ident.span());
                let sig = extract_fn_signature(&method.sig, &method.vis);
                symbols.push(Symbol {
                    name,
                    kind: SymbolKind::Fn,
                    path: path.to_string(),
                    line,
                    signature: sig,
                    crate_name: crate_name.to_string(),
                });
            }
            syn::ImplItem::Type(t) if is_pub_impl(&t.vis) => {
                let name = format!("{}::{}", type_name, t.ident);
                let line = line_of_span(source, t.ident.span());
                let sig = extract_signature(source, line);
                symbols.push(Symbol {
                    name,
                    kind: SymbolKind::Type,
                    path: path.to_string(),
                    line,
                    signature: sig,
                    crate_name: crate_name.to_string(),
                });
            }
            syn::ImplItem::Const(c) if is_pub_impl(&c.vis) => {
                let name = format!("{}::{}", type_name, c.ident);
                let line = line_of_span(source, c.ident.span());
                let sig = extract_signature(source, line);
                symbols.push(Symbol {
                    name,
                    kind: SymbolKind::Const,
                    path: path.to_string(),
                    line,
                    signature: sig,
                    crate_name: crate_name.to_string(),
                });
            }
            _ => {}
        }
    }
}

fn is_pub(vis: &syn::Visibility) -> bool {
    matches!(
        vis,
        syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
    )
}

fn is_pub_impl(vis: &syn::Visibility) -> bool {
    matches!(
        vis,
        syn::Visibility::Public(_) | syn::Visibility::Restricted(_)
    )
}

fn line_of_span(_source: &str, span: proc_macro2::Span) -> usize {
    span.start().line
}

fn extract_signature(source: &str, line: usize) -> String {
    let lines: Vec<&str> = source.lines().collect();
    if line == 0 || line > lines.len() {
        return String::new();
    }

    let raw = lines[line - 1].trim();

    // For struct/enum/trait declarations, we want the declaration line
    // Strip trailing `{` and clean up
    let sig = raw.trim_end_matches('{').trim_end_matches("where").trim();

    truncate_sig(sig)
}

fn extract_fn_signature(sig: &syn::Signature, vis: &syn::Visibility) -> String {
    let vis_str = match vis {
        syn::Visibility::Public(_) => "pub ",
        syn::Visibility::Restricted(_) => "pub(crate) ",
        syn::Visibility::Inherited => "",
    };

    let asyncness = if sig.asyncness.is_some() {
        "async "
    } else {
        ""
    };

    let unsafety = if sig.unsafety.is_some() {
        "unsafe "
    } else {
        ""
    };

    let generics = if sig.generics.params.is_empty() {
        String::new()
    } else {
        let params: Vec<String> = sig
            .generics
            .params
            .iter()
            .map(|p| quote::quote!(#p).to_string())
            .collect();
        format!("<{}>", params.join(", "))
    };

    let args: Vec<String> = sig
        .inputs
        .iter()
        .map(|arg| match arg {
            syn::FnArg::Receiver(r) => {
                let ref_tok = if r.reference.is_some() { "&" } else { "" };
                let mut_tok = if r.mutability.is_some() { "mut " } else { "" };
                format!("{}{}{}", ref_tok, mut_tok, "self")
            }
            syn::FnArg::Typed(pat) => quote::quote!(#pat).to_string(),
        })
        .collect();

    let ret = match &sig.output {
        syn::ReturnType::Default => String::new(),
        syn::ReturnType::Type(_, ty) => {
            format!(" -> {}", quote::quote!(#ty))
        }
    };

    let raw = format!(
        "{}{}{}fn {}{}({}){}",
        vis_str,
        asyncness,
        unsafety,
        sig.ident,
        generics,
        args.join(", "),
        ret
    );

    truncate_sig(&raw)
}

fn truncate_sig(s: &str) -> String {
    if s.len() <= 120 {
        s.to_string()
    } else {
        format!("{}...", &s[..117])
    }
}
