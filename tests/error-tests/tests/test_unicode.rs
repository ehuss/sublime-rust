// Ensures that unicode characters are handled in the JSON output.

fn main() {
    let foo = "❤";
//      ^^^WARN unused variable
//      ^^^NOTE(>=1.17.0) #[warn(unused_variables)]
//      ^^^NOTE(>=1.22.0-nightly) to disable this warning
}
