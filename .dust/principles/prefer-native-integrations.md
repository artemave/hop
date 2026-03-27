# Prefer native integrations

Prefer native APIs, IPC protocols, and embedded extension surfaces over subprocess-driven CLI control.

For `hop`, this means Sway integration should prefer Sway IPC, Kitty integration should prefer kittens and Kitty-native control APIs, and routine shelling out to `swaymsg`, `kitty @`, or similar CLIs is not part of the intended architecture. If a required capability truly has no practical non-CLI interface, isolate that exception behind the adapter layer and document why it exists.

## Parent Principle

(none)

## Sub-Principles

- (none)
