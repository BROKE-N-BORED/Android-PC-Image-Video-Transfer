# Android-PC-Image-Video-Transfer
Quickly scan, select and transfer all image and or video files from your android to your pc

A lightweight Windows program for copying photos, videos from an Android phone to your PC 

## Use it

1. Run `AndroidPhotoTransfer.exe` (in the "Android Photo Transfer (ready to run)" folder).
2. Plug in your phone with a USB **data** cable, unlock it, and choose **File Transfer** if the phone asks.
3. Pick a tab:
   - **Photos & Videos** — thumbnail grid, browse by category (Camera, Screenshots, …) or folder.
   - **Files** — everything else (documents, music, recordings, ZIPs, APKs), browse by file type, quick-access folder or folder.
4. Tick what you want — or use **Transfer All** / **Import New** — pick a folder with **Browse...** and click **TRANSFER**.

- Click to tick, Shift+click to tick a range, Ctrl+A to tick everything in view, double-click to preview.
- A green check means it's already on this PC. The **Show** menu can list only what's *Not on PC yet* (or *Already on PC*),
  and **Untick already on PC** removes transferred items from your selection.
- **Refresh / Rescan Phone** reconnects to the phone and rescans it — no need to unplug.
- While copying you see live **Copied / Skipped / Failed** counts. **Pause** stops right away; the current file restarts on resume.
- Files are never overwritten. A different file with the same name is saved as `name_1.jpg`.
- If the phone disconnects mid-transfer, nothing half-copied is left behind; the program reconnects and **Resume** finishes the rest.

Requires Windows 10/11 and the [.NET 10 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet/10.0)
