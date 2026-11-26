fn main() {
    println!("cargo::rerun-if-changed=c/answer.c");
    cc::Build::new().file("c/answer.c").compile("answer");
}
