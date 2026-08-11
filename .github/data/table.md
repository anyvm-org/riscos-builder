

| Release | armv7 (ARM 32-bit, Cortex-A7) |
|---------|---------|
| 5.30 | ✅ (tar) |

<!-- arch-label: armv7 = armv7 (ARM 32-bit, Cortex-A7) -->

> **Note:** RISC OS support is a **tech preview**. Remote command execution
> and file sync both work, and the desktop comes up and is visible on the VNC
> console. There is **no keyboard** -- see below. Pointer input is untested:
> the ROM does carry a `usbmouse` driver, but no USB mouse has been attached
> to this guest, so nothing here claims it works.

> **Nothing from RISC OS Open is redistributed here.** The builder downloads
> the official Raspberry Pi SD card image from `riscosopen.org` at build time;
> this repository's release assets carry only its own work (the patched QEMU,
> the agent, the injector). Note that ROOL deletes superseded media -- the
> 5.30 zip answers 200 while the identically-shaped 5.28 URL answers 404 --
> so a new release **replaces** this row rather than adding one.

> **No working RISC OS port of QEMU existed, so this builder makes one.**
> `files/qemu-riscos-raspi.patch` is eight fixes plus a new USB NIC model,
> built by `files/build-qemu-riscos.sh` and published as this repo's own
> release asset. Four of the eight are **generic QEMU defects** with nothing
> to do with RISC OS: three in `hcd-dwc2.c` (the frame counter divided
> elapsed time by the frame interval in PHY clocks rather than the frame
> time; `GINTSTS_HCHINT` was raised but never lowered; the bus was started
> from `dwc2_attach()` instead of on the HPRT0 port-enable edge), and one in
> `bcm2835_dma.c`, where `xlen -= 4` on a `uint32_t` byte count wraps for any
> length that is not a multiple of four -- the loop then spins about 2^30
> times, scribbling over guest memory as it goes. The RISC OS specific ones
> are a BCM2835 mailbox power channel, a VCHIQ mailbox peer, byte access on
> the interrupt controller and the BSC/I2C FIFO (RISC OS uses `LDRB`/`STRB`
> where the models demanded word access), and `hw/usb/dev-smsc95xx.c`, a
> model of the SMSC LAN9512 that is the real Pi 2's NIC -- the guest has a
> driver for it and for nothing else QEMU offers.

> **There is no keyboard, and that is a RISC OS limitation.** The BCM2835 ROM
> ships no USB keyboard driver at all: its USB tree has
> `USBDriver/build/c/usbmouse` and nothing for keyboards, the boot prints
> `No keyboard present - autobooting`, and a whole boot's worth of USB traffic
> is `ep0` control transfers with zero interrupt-endpoint packets. Every HID
> device is reported as `[error &24425355]`, and `&24425355` is ASCII `"USB$"`
> -- USBDriver's "attached, no driver" tag. So there is no console to type an
> installer into, and the guest is prepared by patching the disc image offline
> instead (`files/rofilecore.py` parses FileCore's `SBPr` "BigDir" directories
> and overwrites one cosmetic boot script in place, at its existing length).
>
> Three things about that image will bite anyone who touches it. The live boot
> tree **does not exist** in a freshly downloaded image -- RISC OS copies
> `$.!Boot.RO530Hook.Boot` into `$.!Boot.Choices.Boot` on first boot, so
> patching the pristine image patches a template that is never run, and
> patching a booted one lands somewhere else entirely. Every injected command
> must be prefixed with `X` (`*X <cmd>` runs it and discards any error),
> because one error stops the boot on a dialog waiting for a keypress that
> can never come. And injected scripts must print **nothing**: any console
> output from a Tasks hook opens the Wimp's single-tasking output screen,
> which waits for a keypress at the end -- same brick, different cause.

> **The agent.** RISC OS ships **no remote-access server of any kind** --
> checked in a running guest, where `*Modules` lists 137 modules whose only
> networking is the stack (`Internet`, `Resolver`, `DHCP`, `EtherUSB`) and
> clients (`LanManFS` for SMB, `ShareFS`/`Freeway` for Acorn Access). No
> telnetd, no sshd, no ftpd. So, like reactos-builder, this builder supplies
> the server: `files/anyvmd.py`, a small Python agent on port 23
> (`VM_TRANSPORT=telnet`).
>
> The telnet is real, not a raw socket wearing the name: IAC is unescaped
> inbound and doubled outbound, and BINARY (RFC 856) is accepted both ways,
> which is what keeps the tar stream intact. Verified against the worst case
> it can meet -- 4096 bytes of `0xFF` (the IAC byte itself) followed by all
> 256 byte values, round-tripped byte-identical. It is not a *shell*, though:
> RISC OS has no pipes to a child and no `&&`, so the agent parses the two
> tar one-liners itself, services them with Python's `tarfile`, and prints
> the completion marker where a shell would have got it from the chained
> `&& echo`. It also holds a persistent session, several commands down one
> connection, because that is what `telnet_exec` does.
>
> It needs nothing installed -- ROOL's image already ships **Python 2.7.2** at
> `$.Programming.Python.!Python27` with a complete standard library. It runs
> inside a `TaskWindow` so the desktop stays interactive; single-tasking
> Python would freeze the machine for as long as it served, and `*Shutdown`
> would then do nothing at all.
>
> Sync was settled on the running guest rather than assumed. There is no sshd
> and no ssh client (so no rsync / sshfs / scp) and no 9P client. `Fat32FS`
> (1.63) *is* loaded and could in principle carry files on the FAT boot
> partition, but it serves removable media and cannot see the SD card's own
> boot partition -- `::0`, `::4` and `::PiBoot` all resolve to nothing, and
> `Fat32Map`, despite the name, is a DOS-extension-to-filetype table rather
> than a disc mapping.
