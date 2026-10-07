using System.IO;
using System.Windows.Media.Imaging;

namespace AndroidPhotoTransfer.Core.Thumbnails
{
    internal static class ImageDecoder
    {
        /// <summary>
        /// Decodes image bytes to a frozen bitmap no wider than <paramref name="maxPixelWidth"/>, applying EXIF rotation.
        /// Safe to call from any thread. Returns null for formats Windows cannot decode.
        /// </summary>
        public static BitmapSource? Decode(byte[] bytes, int maxPixelWidth)
        {
            try
            {
                int width;
                Rotation rotation;
                using (var probe = new MemoryStream(bytes, writable: false))
                {
                    var frame = BitmapFrame.Create(probe, BitmapCreateOptions.DelayCreation | BitmapCreateOptions.IgnoreColorProfile,
                        BitmapCacheOption.None);
                    width = frame.PixelWidth;
                    rotation = ReadRotation(frame);
                }

                using var stream = new MemoryStream(bytes, writable: false);
                var image = new BitmapImage();
                image.BeginInit();
                image.CacheOption = BitmapCacheOption.OnLoad;
                image.CreateOptions = BitmapCreateOptions.IgnoreColorProfile;
                image.StreamSource = stream;
                if (maxPixelWidth > 0 && width > maxPixelWidth) image.DecodePixelWidth = maxPixelWidth;
                image.Rotation = rotation;
                image.EndInit();
                image.Freeze();
                return image;
            }
            catch
            {
                return null;
            }
        }

        /// <summary>Original pixel size as the viewer sees it (rotation applied), or (0, 0) if unknown.</summary>
        public static (int Width, int Height) ReadPixelSize(byte[] bytes)
        {
            try
            {
                using var probe = new MemoryStream(bytes, writable: false);
                var frame = BitmapFrame.Create(probe, BitmapCreateOptions.DelayCreation | BitmapCreateOptions.IgnoreColorProfile,
                    BitmapCacheOption.None);
                var rotation = ReadRotation(frame);
                return rotation is Rotation.Rotate90 or Rotation.Rotate270
                    ? (frame.PixelHeight, frame.PixelWidth)
                    : (frame.PixelWidth, frame.PixelHeight);
            }
            catch
            {
                return (0, 0);
            }
        }

        private static Rotation ReadRotation(BitmapFrame frame)
        {
            try
            {
                if (frame.Metadata is BitmapMetadata metadata &&
                    metadata.ContainsQuery("System.Photo.Orientation") &&
                    metadata.GetQuery("System.Photo.Orientation") is ushort orientation)
                {
                    return orientation switch
                    {
                        3 => Rotation.Rotate180,
                        6 => Rotation.Rotate90,
                        8 => Rotation.Rotate270,
                        _ => Rotation.Rotate0
                    };
                }
            }
            catch
            {
                // Formats without EXIF metadata (PNG, GIF) throw here.
            }
            return Rotation.Rotate0;
        }
    }
}
