use std::path::Path;

/// Helper: run the extractor on inline Rust source and return symbols
/// Returns (name, kind, path, signature) tuples.
fn extract_source(source: &str) -> Vec<(String, String, String, String)> {
    let dir = tempfile::tempdir().unwrap();
    let ws = dir.path();

    std::fs::write(
        ws.join("Cargo.toml"),
        r#"[workspace]
members = ["test_crate"]
"#,
    )
    .unwrap();

    let crate_dir = ws.join("test_crate");
    std::fs::create_dir_all(crate_dir.join("src")).unwrap();
    std::fs::write(
        crate_dir.join("Cargo.toml"),
        r#"[package]
name = "test_crate"
version = "0.1.0"
edition = "2021"
"#,
    )
    .unwrap();
    std::fs::write(crate_dir.join("src/lib.rs"), source).unwrap();

    let output_dir = ws.join("index");
    let binary = env!("CARGO_BIN_EXE_rust-symbols");
    let output = std::process::Command::new(binary)
        .arg(ws.to_str().unwrap())
        .arg("--output")
        .arg(output_dir.to_str().unwrap())
        .arg("--stats")
        .output()
        .expect("failed to run rust-index");

    assert!(
        output.status.success(),
        "rust-index failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    // Parse symbols.txt — format: name|kind|path|signature
    let symbols_content = std::fs::read_to_string(output_dir.join("symbols.txt")).unwrap();
    symbols_content
        .lines()
        .filter(|l| !l.starts_with('#') && !l.is_empty())
        .map(|line| {
            let parts: Vec<&str> = line.splitn(4, '|').collect();
            assert!(parts.len() == 4, "bad line: {}", line);
            (
                parts[0].to_string(), // name
                parts[1].to_string(), // kind
                parts[2].to_string(), // path
                parts[3].to_string(), // signature
            )
        })
        .collect()
}

fn find_symbol<'a>(
    symbols: &'a [(String, String, String, String)],
    name: &str,
) -> Option<&'a (String, String, String, String)> {
    symbols.iter().find(|s| s.0 == name)
}

// ─── Extraction tests ───

#[test]
fn test_extract_pub_struct() {
    let syms = extract_source(
        r#"
pub struct Foo {
    pub x: u32,
}

struct Private {
    y: u32,
}
"#,
    );

    assert!(
        find_symbol(&syms, "Foo").is_some(),
        "should find pub struct Foo"
    );
    let foo = find_symbol(&syms, "Foo").unwrap();
    assert_eq!(foo.1, "struct");
    assert!(foo.3.contains("pub struct Foo"));

    assert!(
        find_symbol(&syms, "Private").is_none(),
        "should NOT find private struct"
    );
}

#[test]
fn test_extract_pub_enum() {
    let syms = extract_source(
        r#"
pub enum Color {
    Red,
    Green,
    Blue,
}
"#,
    );

    let color = find_symbol(&syms, "Color").unwrap();
    assert_eq!(color.1, "enum");
    assert!(color.3.contains("pub enum Color"));
}

#[test]
fn test_extract_pub_trait() {
    let syms = extract_source(
        r#"
pub trait Drawable {
    fn draw(&self);
}
"#,
    );

    let t = find_symbol(&syms, "Drawable").unwrap();
    assert_eq!(t.1, "trait");
}

#[test]
fn test_extract_pub_fn() {
    let syms = extract_source(
        r#"
pub fn hello(name: &str) -> String {
    format!("hello {}", name)
}

fn private_fn() {}
"#,
    );

    let hello = find_symbol(&syms, "hello").unwrap();
    assert_eq!(hello.1, "fn");
    assert!(hello.3.contains("pub fn hello"));
    assert!(hello.3.contains("-> String"));

    assert!(find_symbol(&syms, "private_fn").is_none());
}

#[test]
fn test_extract_pub_const_and_static() {
    let syms = extract_source(
        r#"
pub const MAX_SIZE: usize = 100;
pub static COUNTER: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
const PRIVATE_CONST: u32 = 42;
"#,
    );

    assert!(find_symbol(&syms, "MAX_SIZE").is_some());
    assert_eq!(find_symbol(&syms, "MAX_SIZE").unwrap().1, "const");

    assert!(find_symbol(&syms, "COUNTER").is_some());
    assert_eq!(find_symbol(&syms, "COUNTER").unwrap().1, "static");

    assert!(find_symbol(&syms, "PRIVATE_CONST").is_none());
}

#[test]
fn test_extract_pub_type_alias() {
    let syms = extract_source(
        r#"
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
"#,
    );

    let t = find_symbol(&syms, "Result").unwrap();
    assert_eq!(t.1, "type");
}

#[test]
fn test_extract_impl_methods() {
    let syms = extract_source(
        r#"
pub struct MyStruct;

impl MyStruct {
    pub fn new() -> Self {
        MyStruct
    }

    pub fn do_thing(&self, x: u32) -> bool {
        x > 0
    }

    fn private_method(&self) {}
}
"#,
    );

    assert!(find_symbol(&syms, "MyStruct").is_some());

    let new = find_symbol(&syms, "MyStruct::new").unwrap();
    assert_eq!(new.1, "fn");
    assert!(new.3.contains("pub fn new"));

    let do_thing = find_symbol(&syms, "MyStruct::do_thing").unwrap();
    assert!(do_thing.3.contains("&self"));
    assert!(do_thing.3.contains("-> bool"));

    assert!(
        find_symbol(&syms, "MyStruct::private_method").is_none(),
        "should NOT find private method"
    );
}

#[test]
fn test_extract_async_fn() {
    let syms = extract_source(
        r#"
pub async fn fetch_data(url: &str) -> Result<String, ()> {
    Ok(String::new())
}
"#,
    );

    let f = find_symbol(&syms, "fetch_data").unwrap();
    assert!(f.3.contains("async"), "signature should include async");
    assert!(f.3.contains("pub async fn fetch_data"));
}

#[test]
fn test_extract_generic_struct() {
    let syms = extract_source(
        r#"
pub struct Container<T: Clone + Send> {
    inner: T,
}
"#,
    );

    let c = find_symbol(&syms, "Container").unwrap();
    assert!(c.3.contains("pub struct Container"));
}

#[test]
fn test_extract_pub_mod() {
    let syms = extract_source(
        r#"
pub mod networking {
    pub fn connect() {}
}
"#,
    );

    assert!(find_symbol(&syms, "networking").is_some());
    assert_eq!(find_symbol(&syms, "networking").unwrap().1, "mod");
}

#[test]
fn test_signature_truncation() {
    // Create a function with a very long signature (> 120 chars)
    let long_params = (0..20)
        .map(|i| format!("param_{}: &'static str", i))
        .collect::<Vec<_>>()
        .join(", ");
    let source = format!("pub fn long_fn({}) {{}}", long_params);

    let syms = extract_source(&source);
    let f = find_symbol(&syms, "long_fn").unwrap();
    assert!(
        f.3.len() <= 123,
        "signature should be truncated to ~120 chars, got {}",
        f.3.len()
    );
    assert!(f.3.ends_with("..."), "truncated sig should end with ...");
}

#[test]
fn test_symbols_sorted_alphabetically() {
    let syms = extract_source(
        r#"
pub struct Zebra;
pub struct Apple;
pub struct Mango;
pub fn banana() {}
"#,
    );

    let names: Vec<&str> = syms.iter().map(|s| s.0.as_str()).collect();
    let mut sorted = names.clone();
    sorted.sort();
    assert_eq!(names, sorted, "symbols should be alphabetically sorted");
}

// ─── Output format tests ───

#[test]
fn test_crates_idx_format() {
    let dir = tempfile::tempdir().unwrap();
    let ws = dir.path();

    std::fs::write(
        ws.join("Cargo.toml"),
        r#"[workspace]
members = ["crate_a", "crate_b"]
"#,
    )
    .unwrap();

    for name in &["crate_a", "crate_b"] {
        let d = ws.join(name);
        std::fs::create_dir_all(d.join("src")).unwrap();
        std::fs::write(
            d.join("Cargo.toml"),
            format!(
                r#"[package]
name = "{}"
version = "0.1.0"
edition = "2021"
"#,
                name
            ),
        )
        .unwrap();
        std::fs::write(d.join("src/lib.rs"), "pub fn hello() {}").unwrap();
    }

    let output_dir = ws.join("index");
    let binary = env!("CARGO_BIN_EXE_rust-symbols");
    let output = std::process::Command::new(binary)
        .arg(ws.to_str().unwrap())
        .arg("--output")
        .arg(output_dir.to_str().unwrap())
        .output()
        .unwrap();
    assert!(output.status.success());

    let crates = std::fs::read_to_string(output_dir.join("crates.txt")).unwrap();
    let lines: Vec<&str> = crates.lines().collect();

    // Header lines
    assert!(lines[0].starts_with("# rust-symbols crates"));
    assert!(lines[0].contains("2 crates"));
    assert_eq!(lines[1], "# crate|path|deps");

    // Data lines
    assert!(lines[2].starts_with("crate_a|"));
    assert!(lines[3].starts_with("crate_b|"));
}

#[test]
fn test_modules_idx_format() {
    let dir = tempfile::tempdir().unwrap();
    let ws = dir.path();

    std::fs::write(
        ws.join("Cargo.toml"),
        r#"[workspace]
members = ["mylib"]
"#,
    )
    .unwrap();

    let d = ws.join("mylib");
    std::fs::create_dir_all(d.join("src/sub")).unwrap();
    std::fs::write(
        d.join("Cargo.toml"),
        r#"[package]
name = "mylib"
version = "0.1.0"
edition = "2021"
"#,
    )
    .unwrap();
    std::fs::write(d.join("src/lib.rs"), "pub mod sub;\npub fn root_fn() {}").unwrap();
    std::fs::write(d.join("src/sub/mod.rs"), "pub fn sub_fn() {}").unwrap();

    let output_dir = ws.join("index");
    let binary = env!("CARGO_BIN_EXE_rust-symbols");
    let output = std::process::Command::new(binary)
        .arg(ws.to_str().unwrap())
        .arg("--output")
        .arg(output_dir.to_str().unwrap())
        .output()
        .unwrap();
    assert!(output.status.success());

    let modules = std::fs::read_to_string(output_dir.join("modules.txt")).unwrap();
    let data_lines: Vec<&str> = modules.lines().filter(|l| !l.starts_with('#')).collect();

    // Should have entries for lib.rs and sub/mod.rs
    assert!(
        data_lines
            .iter()
            .any(|l| l.contains("mylib||") || l.contains("mylib|lib|")),
        "should have root module entry: {:?}",
        data_lines
    );
    assert!(
        data_lines.iter().any(|l| l.contains("|sub|")),
        "should have sub module entry: {:?}",
        data_lines
    );
}

// ─── Integration test on Lighthouse ───

#[test]
fn test_lighthouse_integration() {
    let lighthouse_path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("lighthouse");

    if !lighthouse_path.join("Cargo.toml").exists() {
        eprintln!("Skipping lighthouse integration test: lighthouse not found");
        return;
    }

    let output_dir = tempfile::tempdir().unwrap();
    let binary = env!("CARGO_BIN_EXE_rust-symbols");
    let output = std::process::Command::new(binary)
        .arg(lighthouse_path.to_str().unwrap())
        .arg("--output")
        .arg(output_dir.path().to_str().unwrap())
        .arg("--stats")
        .output()
        .unwrap();

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        output.status.success(),
        "rust-index on lighthouse failed:\nstdout: {}\nstderr: {}",
        stdout,
        stderr
    );

    // Verify all 3 files exist
    assert!(output_dir.path().join("crates.txt").exists());
    assert!(output_dir.path().join("symbols.txt").exists());
    assert!(output_dir.path().join("modules.txt").exists());

    // Parse stats from stdout
    assert!(stdout.contains("Crates:"), "should print crate count");
    assert!(stdout.contains("Symbols:"), "should print symbol count");

    // Verify symbol count is reasonable (lighthouse has thousands)
    let symbols = std::fs::read_to_string(output_dir.path().join("symbols.txt")).unwrap();
    let symbol_count = symbols
        .lines()
        .filter(|l| !l.starts_with('#') && !l.is_empty())
        .count();
    assert!(
        symbol_count > 5000,
        "lighthouse should have >5000 pub symbols, got {}",
        symbol_count
    );

    // Verify key types exist
    assert!(symbols.contains("BeaconChain|struct|"));
    assert!(symbols.contains("BeaconState|struct|"));
    assert!(
        symbols.contains("BeaconChain::import_block|fn|")
            || symbols.contains("BeaconChain::process_block|fn|"),
        "should have BeaconChain methods"
    );

    // Verify crates count
    let crates = std::fs::read_to_string(output_dir.path().join("crates.txt")).unwrap();
    let crate_count = crates
        .lines()
        .filter(|l| !l.starts_with('#') && !l.is_empty())
        .count();
    assert!(
        crate_count > 50,
        "lighthouse should have >50 crates, got {}",
        crate_count
    );

    // Verify alphabetical sorting
    let names: Vec<&str> = symbols
        .lines()
        .filter(|l| !l.starts_with('#') && !l.is_empty())
        .filter_map(|l| l.split('|').next())
        .collect();
    let mut sorted = names.clone();
    sorted.sort();
    assert_eq!(names, sorted, "symbols should be alphabetically sorted");
}

// ─── Edge cases ───

#[test]
fn test_empty_crate() {
    let syms = extract_source("");
    assert!(syms.is_empty(), "empty source should produce no symbols");
}

#[test]
fn test_only_private_items() {
    let syms = extract_source(
        r#"
struct Private;
fn helper() {}
const SECRET: u32 = 42;
"#,
    );
    assert!(
        syms.is_empty(),
        "all-private source should produce no symbols"
    );
}

#[test]
fn test_mixed_visibility() {
    let syms = extract_source(
        r#"
pub struct Public;
pub(crate) struct CrateLevel;
pub(super) struct SuperLevel;
struct Private;
"#,
    );

    // pub and pub(crate)/pub(super) should appear, but not private
    assert_eq!(
        syms.len(),
        3,
        "pub + pub(crate) + pub(super) should appear, got: {:?}",
        syms
    );
    let names: Vec<&str> = syms.iter().map(|s| s.0.as_str()).collect();
    assert!(names.contains(&"Public"));
    assert!(names.contains(&"CrateLevel"));
    assert!(names.contains(&"SuperLevel"));
}

#[test]
fn test_trait_impl_methods_excluded() {
    let syms = extract_source(
        r#"
pub struct MyType;

pub trait MyTrait {
    fn required(&self);
}

impl MyTrait for MyType {
    fn required(&self) {}
}

impl MyType {
    pub fn inherent(&self) {}
}
"#,
    );

    // Should have: MyType (struct), MyTrait (trait), MyType::inherent (fn)
    // Should NOT have trait impl methods (they're not independently pub)
    assert!(find_symbol(&syms, "MyType").is_some());
    assert!(find_symbol(&syms, "MyTrait").is_some());
    assert!(find_symbol(&syms, "MyType::inherent").is_some());
    assert!(
        find_symbol(&syms, "MyType::required").is_none(),
        "trait impl method 'required' should not appear (not pub)"
    );
}
