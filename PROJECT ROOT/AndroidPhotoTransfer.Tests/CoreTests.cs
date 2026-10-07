using System.IO;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Thumbnails;
using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Data;
using AndroidPhotoTransfer.Infrastructure;

namespace AndroidPhotoTransfer.Tests;

public class MediaScannerTests
{
    [Fact]
    public async Task Finds_photos_and_videos_and_skips_app_data_hidden_folders_and_other_files()
    {
        var phone = new FakePhone();
        phone.AddFile("DCIM/Camera/IMG_1.jpg", TestData.Bytes(10));
        phone.AddFile("DCIM/Camera/VID_1.MP4", TestData.Bytes(10));
        phone.AddFile("DCIM/.thumbnails/123.jpg", TestData.Bytes(10));
        phone.AddFile("Pictures/Screenshots/Screenshot_1.png", TestData.Bytes(10));
        phone.AddFile("Download/report.pdf", TestData.Bytes(10));
        phone.AddFile("Download/meme.webp", TestData.Bytes(10));
        phone.AddFile("Android/data/com.app/cache/x.jpg", TestData.Bytes(10));
        phone.AddFile("Android/media/com.whatsapp/WhatsApp/Media/IMG-WA0001.jpg", TestData.Bytes(10));

        var found = new List<MediaItem>();
        var roots = await phone.GetStorageRootsAsync(default);
        await new MediaScanner().ScanAsync(phone, roots, batch => found.AddRange(batch), default);

        var names = found.Select(f => f.Name).OrderBy(n => n, StringComparer.Ordinal).ToArray();
        // Every visible file is found (the Files tab needs them); app data and hidden folders are skipped.
        Assert.Equal(new[] { "IMG-WA0001.jpg", "IMG_1.jpg", "Screenshot_1.png", "VID_1.MP4", "meme.webp", "report.pdf" }, names);
        Assert.Equal(MediaKind.File, found.Single(f => f.Name == "report.pdf").Kind);
        Assert.Equal(FileCategory.Document, found.Single(f => f.Name == "report.pdf").FileCategory);
        Assert.All(found, f => Assert.NotNull(f.PersistentId));

        Assert.Equal(MediaCategory.Camera, found.Single(f => f.Name == "IMG_1.jpg").Category);
        Assert.Equal(MediaKind.Video, found.Single(f => f.Name == "VID_1.MP4").Kind);
        Assert.Equal(MediaCategory.Screenshots, found.Single(f => f.Name == "Screenshot_1.png").Category);
        Assert.Equal(MediaCategory.Downloads, found.Single(f => f.Name == "meme.webp").Category);
        Assert.Equal(MediaCategory.Other, found.Single(f => f.Name == "IMG-WA0001.jpg").Category);
        Assert.Equal("DCIM/Camera", found.Single(f => f.Name == "IMG_1.jpg").Folder);
    }
}

public class FileCategoryTests
{
    [Theory]
    [InlineData("a.jpg", FileCategory.Photo)]
    [InlineData("b.MP4", FileCategory.Video)]
    [InlineData("c.pdf", FileCategory.Document)]
    [InlineData("d.mp3", FileCategory.Audio)]
    [InlineData("e.zip", FileCategory.Archive)]
    [InlineData("f.apk", FileCategory.App)]
    [InlineData("g.xyz", FileCategory.Other)]
    [InlineData("noextension", FileCategory.Other)]
    public void Classifies_files_for_the_files_tab(string name, FileCategory expected) =>
        Assert.Equal(expected, MediaClassifier.GetFileCategory(name, MediaClassifier.GetKind(name) ?? MediaKind.File));
}

public class MediaQueryTests
{
    private static MediaItem Item(string name, long size, int day, MediaKind kind = MediaKind.Photo, string folder = "DCIM/Camera") =>
        new($@"\S\{folder}\{name}", name, "S", folder, $@"\S\{folder}", size, null, new DateTime(2026, 10, day), kind);

    [Fact]
    public void Sorts_filters_and_searches()
    {
        var items = new[]
        {
            Item("b.jpg", 300, 2),
            Item("a.jpg", 100, 3),
            Item("c.mp4", 200, 1, MediaKind.Video),
            Item("Screenshot_1.png", 50, 4, folder: "Pictures/Screenshots")
        };

        Assert.Equal(new[] { "Screenshot_1.png", "a.jpg", "b.jpg", "c.mp4" },
            MediaQuery.Apply(items, x => x, null, MediaTypeFilter.All, null, SortMode.NewestFirst).Select(x => x.Name));
        Assert.Equal(new[] { "b.jpg", "c.mp4", "a.jpg", "Screenshot_1.png" },
            MediaQuery.Apply(items, x => x, null, MediaTypeFilter.All, null, SortMode.LargestFirst).Select(x => x.Name));
        Assert.Equal(new[] { "c.mp4" },
            MediaQuery.Apply(items, x => x, null, MediaTypeFilter.Videos, null, SortMode.NameAscending).Select(x => x.Name));
        Assert.Equal(new[] { "Screenshot_1.png" },
            MediaQuery.Apply(items, x => x, null, MediaTypeFilter.All, "screenshots", SortMode.NameAscending).Select(x => x.Name));
        Assert.Equal(new[] { "a.jpg", "b.jpg" },
            MediaQuery.Apply(items, x => x, null, MediaTypeFilter.Photos, ".jpg", SortMode.NameAscending).Select(x => x.Name));
    }
}

public class ThumbnailCacheTests
{
    private static BitmapSource Bitmap(int size)
    {
        var bitmap = BitmapSource.Create(size, size, 96, 96, PixelFormats.Bgra32, null, new byte[size * size * 4], size * 4);
        bitmap.Freeze();
        return bitmap;
    }

    [Fact]
    public void Evicts_least_recently_used_when_over_budget()
    {
        long one = 100 * 100 * 4;
        var cache = new ThumbnailCache(one * 3);
        cache.Add("a", Bitmap(100));
        cache.Add("b", Bitmap(100));
        cache.Add("c", Bitmap(100));
        Assert.NotNull(cache.Get("a")); // touch "a" so "b" is now the oldest

        cache.Add("d", Bitmap(100));

        Assert.Null(cache.Get("b"));
        Assert.NotNull(cache.Get("a"));
        Assert.NotNull(cache.Get("c"));
        Assert.NotNull(cache.Get("d"));
        Assert.True(cache.CurrentBytes <= cache.CapacityBytes);
    }

    [Fact]
    public void Shrinking_capacity_trims_immediately()
    {
        var cache = new ThumbnailCache(10_000_000);
        for (int i = 0; i < 10; i++) cache.Add(i.ToString(), Bitmap(100));
        cache.CapacityBytes = 100 * 100 * 4 * 2;
        Assert.Equal(2, cache.Count);
    }
}

public class ImageDecoderTests
{
    [Fact]
    public void Decodes_and_downscales_a_jpeg()
    {
        var source = BitmapSource.Create(800, 600, 96, 96, PixelFormats.Bgr32, null, new byte[800 * 600 * 4], 800 * 4);
        var encoder = new JpegBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(source));
        using var stream = new MemoryStream();
        encoder.Save(stream);

        var decoded = ImageDecoder.Decode(stream.ToArray(), 200);

        Assert.NotNull(decoded);
        Assert.Equal(200, decoded!.PixelWidth);
        Assert.True(decoded.IsFrozen);
        Assert.Equal((800, 600), ImageDecoder.ReadPixelSize(stream.ToArray()));
    }

    [Fact]
    public void Returns_null_for_garbage()
    {
        Assert.Null(ImageDecoder.Decode(new byte[] { 1, 2, 3, 4 }, 200));
    }
}

public class HistoryDatabaseTests
{
    [Fact]
    public void Remembers_transfers_per_device_and_ignores_duplicates()
    {
        using var folder = new TempFolder();
        var path = Path.Combine(folder.Path, "history.db");
        var item = new MediaItem(@"\S\DCIM\IMG.jpg", "IMG.jpg", "S", "DCIM", @"\S\DCIM", 1234, null, DateTime.Now, MediaKind.Photo);

        using (var db = new HistoryDatabase(path))
        {
            db.Record("SN:1", item, @"C:\x\IMG.jpg");
            db.Record("SN:1", item, @"C:\x\IMG.jpg"); // same file twice → one row
        }

        using (var db = new HistoryDatabase(path))
        {
            Assert.Equal(new[] { item.HistoryKey }, db.GetTransferredKeys("SN:1"));
            Assert.Empty(db.GetTransferredKeys("SN:2"));
        }
    }
}

public class LoggerTests
{
    [Fact]
    public void Keeps_logging_after_the_log_file_rolls_over()
    {
        using var folder = new TempFolder();
        Logger.Initialize(folder.Path);
        var filler = new string('x', 2000);
        for (int i = 0; i < 700; i++) Logger.Info(filler); // ~1.4 MB, forces a rollover
        Logger.Info("AFTER-ROLLOVER");

        var current = File.ReadAllText(Path.Combine(folder.Path, "app.log"));
        Assert.Contains("AFTER-ROLLOVER", current);
        Assert.True(File.Exists(Path.Combine(folder.Path, "app.log.1")));
        Assert.True(new FileInfo(Path.Combine(folder.Path, "app.log")).Length < 1024 * 1024);
    }
}

public class UtilityTests
{
    [Theory]
    [InlineData("IMG:001?.jpg", "IMG_001_.jpg")]
    [InlineData("normal.jpg", "normal.jpg")]
    [InlineData("   ", "unnamed")]
    public void Sanitizes_phone_file_names(string input, string expected) =>
        Assert.Equal(expected, FileUtilities.SanitizeFileName(input));

    [Fact]
    public void Relative_folders_cannot_escape_the_destination() =>
        Assert.Equal(Path.Combine("DCIM", "Camera"), FileUtilities.SanitizeRelativeFolder("../DCIM/./Camera"));

    [Theory]
    [InlineData(500, "500 B")]
    [InlineData(1536, "1.5 KB")]
    [InlineData(2_576_980_378, "2.4 GB")]
    public void Formats_sizes(long bytes, string expected) => Assert.Equal(expected, FileUtilities.FormatBytes(bytes));

    [Theory]
    [InlineData(@"\\?\swd#wpdbusenum#{4f246789}#0000000000100000#{6ac27878-a6fa-4155-ba85-f98f491d4f33}", true)]
    [InlineData(@"\\?\usb#vid_04e8&pid_6860&ms_comp_mtp&samsung_android#6&1234&0&0000#{6ac27878-a6fa-4155-ba85-f98f491d4f33}", false)]
    public void Recognises_usb_drives_that_are_not_phones(string deviceId, bool isDrive) =>
        Assert.Equal(isDrive, WpdDeviceList.IsMassStorageVolume(deviceId));

    [Fact]
    public void Settings_round_trip()
    {
        using var folder = new TempFolder();
        var path = Path.Combine(folder.Path, "settings.json");
        var manager = new SettingsManager(path);
        manager.Current.LastDestination = @"D:\Photos";
        manager.Current.SortMode = SortMode.LargestFirst;
        manager.Save();

        var reloaded = new SettingsManager(path).Current;
        Assert.Equal(@"D:\Photos", reloaded.EffectiveDestination);
        Assert.Equal(SortMode.LargestFirst, reloaded.SortMode);
    }
}
