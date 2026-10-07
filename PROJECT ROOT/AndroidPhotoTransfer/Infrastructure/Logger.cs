using System.Diagnostics;
using System.IO;

namespace AndroidPhotoTransfer.Infrastructure
{
    /// <summary>
    /// Small rolling file logger (app.log, app.log.1, app.log.2; 1 MB each).
    /// Logging must never crash or slow the app, so every failure is swallowed.
    /// </summary>
    internal static class Logger
    {
        private const long MaxFileBytes = 1024 * 1024;
        private const int KeptFiles = 3;
        private static readonly object Gate = new();
        private static string? _logFile;

        public static string? LogFile => _logFile;

        public static void Initialize(string folder)
        {
            try
            {
                Directory.CreateDirectory(folder);
                _logFile = Path.Combine(folder, "app.log");
            }
            catch
            {
                _logFile = null;
            }
        }

        public static void Info(string message) => Write("INFO ", message);

        public static void Warn(string message, Exception? ex = null) =>
            Write("WARN ", ex == null ? message : $"{message} | {ex.GetType().Name}: {ex.Message} (0x{ex.HResult:X8})");

        public static void Error(string message, Exception? ex = null) =>
            Write("ERROR", ex == null ? message : $"{message}{Environment.NewLine}{ex}");

        private static void Write(string level, string message)
        {
            var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {level} {message}{Environment.NewLine}";
            Debug.Write(line);

            var file = _logFile;
            if (file == null) return;

            lock (Gate)
            {
                try
                {
                    var info = new FileInfo(file);
                    if (info.Exists && info.Length > MaxFileBytes) Rotate(file);
                    File.AppendAllText(file, line);
                }
                catch
                {
                    // Ignore: a full or read-only disk must not take the app down.
                }
            }
        }

        private static void Rotate(string file)
        {
            for (int i = KeptFiles - 1; i >= 1; i--)
            {
                var older = $"{file}.{i}";
                var newer = i == 1 ? file : $"{file}.{i - 1}";
                if (File.Exists(older)) File.Delete(older);
                if (File.Exists(newer)) File.Move(newer, older);
            }
        }
    }
}
