use std::ffi::{CStr, c_char};
use std::net::TcpListener;
use std::thread;

unsafe extern "C" {
    fn answer(text: *const c_char) -> i32;
}

fn ask(text: &CStr) -> i32 {
    // SAFETY: `answer` only reads the NUL-terminated string it is given.
    unsafe { answer(text.as_ptr()) }
}

fn main() -> std::io::Result<()> {
    // A socket and a thread make the executable link what the platform keeps them in.
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let asked = thread::spawn(|| ask(c"answer"));
    let answered = asked
        .join()
        .map_err(|_| std::io::Error::other("the thread panicked"))?;
    drop(listener);
    println!("answer {answered}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::ask;

    #[test]
    fn the_c_object_is_linked() {
        assert_eq!(ask(c"answer"), 42);
    }
}
