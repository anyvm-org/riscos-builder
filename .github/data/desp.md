How the images are built:

Each image is built automatically in the
[anyvm-org/riscos-builder](https://github.com/anyvm-org/riscos-builder)
repo's GitHub Actions: it downloads RISC OS Open's official Raspberry
Pi distribution, prepares the SD-card image offline (remote access and
the anyvm runtime support are injected into the image), verifies it by
booting in QEMU, and exports the disk image. No interactive installer
is run.

Upstream media: the official RISC OS Open Raspberry Pi downloads from
https://www.riscosopen.org/content/downloads/raspberry-pi.
