# Android-PC-Image-Video-Transfer
Quickly scan, select and transfer all image and or video files from your android to your pc

A lightweight Windows program for copying photos, videos and other files from an Android phone to your PC over a USB cable.
Nothing is installed on the phone.

## Use it

1. Run `AndroidPhotoTransfer.exe` (in the "Android Photo Transfer (ready to run)" folder).
2. Plug in your phone with a USB **data** cable, unlock it, and choose **File Transfer** if the phone asks.
3. Pick a tab:
   - **Photos & Videos** — thumbnail grid, browse by category (Camera, Screenshots, …) or folder.
   - **Files** — every file on the phone (documents, music, downloads, ZIPs, APKs…), same layout.
4. Tick what you want — or use **Transfer All** / **Import New** — pick a folder with **Browse...** and click **TRANSFER**.

- Click to tick, Shift+click to tick a range, Ctrl+A to tick everything in view, double-click to preview.
- A green check means it's already on this PC. The **Show** menu can list only what's *Not on PC yet* (or *Already on PC*),
  and **Untick already on PC** removes transferred items from your selection.
- **Refresh / Rescan Phone** reconnects to the phone and rescans it — no need to unplug.
- While copying you see live **Copied / Skipped / Failed** counts. **Pause** stops right away; the current file restarts on resume.
- Files are never overwritten. A different file with the same name is saved as `name_1.jpg`.
- If the phone disconnects mid-transfer, nothing half-copied is left behind; the program reconnects and **Resume** finishes the rest.

Requires Windows 10/11 and the [.NET 10 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet/10.0) (already installed on this PC).

Settings, transfer history and logs live in `%LOCALAPPDATA%\AndroidPhotoTransfer`.

## Develop

```
dotnet build   PROJECT ROOT\AndroidPhotoTransfer.slnx
dotnet test    PROJECT ROOT\AndroidPhotoTransfer.slnx
dotnet publish PROJECT ROOT\AndroidPhotoTransfer\AndroidPhotoTransfer.csproj -c Release -o "Android Photo Transfer (ready to run)"
```

| Folder | Contents |
|---|---|
| `Core/Wpd` | All phone (WPD/MTP) access, on one background thread (`MtpWorker`) |
| `Core/Devices` | Phone detection and connection state (event-driven via `WM_DEVICECHANGE`) |
| `Core/Media` | Scanning, categories, sort/filter/search |
| `Core/Thumbnails` | On-screen-only thumbnail loading with a 64 MB LRU cache |
| `Core/Transfer` | Streaming copy engine: `.partial` → size check → rename, skip/rename duplicates, pause/cancel/resume |
| `Data` | SQLite transfer history (for Import New) |
| `UI` | WPF view models and windows |

Testing without a phone:
- `APT_DEMO_FOLDER=C:\some\folder` treats a normal folder as a phone ("Demo Phone").
- `APT_INCLUDE_STORAGE_DEVICES=1` also lists USB drives (they use the same Windows WPD interface).
- `APT_TEST_REAL_DEVICE=1` enables the real-device integration test.
