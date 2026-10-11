#!/bin/sh
# Recompila el motor de LightCraft para el navegador y lo deja en ../static/lightcraft/.
# Requisitos: rustup target add wasm32-unknown-unknown
#             cargo install wasm-bindgen-cli --version 0.2.129 --locked
set -e
cd "$(dirname "$0")"
cargo test --release
cargo build --release --target wasm32-unknown-unknown
wasm-bindgen target/wasm32-unknown-unknown/release/geotagger_lightcraft.wasm \
  --target web --no-typescript --out-dir ../static/lightcraft
ls -la ../static/lightcraft
