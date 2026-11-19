pub mod engine;
pub mod evidence;
pub mod experiment;
pub mod fixture;
pub mod shadow;

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

pub fn digest(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}
