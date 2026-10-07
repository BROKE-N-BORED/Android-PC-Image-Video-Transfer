using System.IO;
using AndroidPhotoTransfer.Core.Media;
using SQLite;

namespace AndroidPhotoTransfer.Data
{
    /// <summary>SQLite-backed transfer history in %LOCALAPPDATA%\AndroidPhotoTransfer\history.db.</summary>
    internal sealed class HistoryDatabase : IHistoryDatabase, IDisposable
    {
        private readonly SQLiteConnection _db;
        private readonly object _gate = new();

        public HistoryDatabase(string path)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            _db = new SQLiteConnection(path);
            // Write-ahead logging: recording each copied file costs well under a millisecond instead of a disk flush.
            _db.ExecuteScalar<string>("PRAGMA journal_mode=WAL");
            _db.Execute("PRAGMA synchronous=NORMAL");
            _db.CreateTable<TransferRecord>();
        }

        public void Record(string deviceKey, MediaItem item, string destination)
        {
            lock (_gate)
            {
                _db.Execute(
                    "INSERT OR REPLACE INTO Transfers (DeviceKey, SourcePath, SourceSize, SourceModified, Destination, TransferredAt) " +
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    deviceKey, item.Path, item.Size, item.Date?.Ticks, destination, DateTime.Now.Ticks);
            }
        }

        public HashSet<string> GetTransferredKeys(string deviceKey)
        {
            lock (_gate)
            {
                var rows = _db.Query<TransferRecord>(
                    "SELECT SourcePath, SourceSize FROM Transfers WHERE DeviceKey = ?", deviceKey);
                return rows.Select(r => $"{r.SourcePath}|{r.SourceSize}").ToHashSet(StringComparer.Ordinal);
            }
        }

        public void Dispose()
        {
            lock (_gate) _db.Dispose();
        }

        [Table("Transfers")]
        public sealed class TransferRecord
        {
            [PrimaryKey, AutoIncrement]
            public int Id { get; set; }

            [Indexed(Name = "IX_Transfers_Source", Order = 1, Unique = true)]
            public string DeviceKey { get; set; } = "";

            [Indexed(Name = "IX_Transfers_Source", Order = 2, Unique = true)]
            public string SourcePath { get; set; } = "";

            [Indexed(Name = "IX_Transfers_Source", Order = 3, Unique = true)]
            public long SourceSize { get; set; }

            public DateTime? SourceModified { get; set; }
            public string Destination { get; set; } = "";
            public DateTime TransferredAt { get; set; }
        }
    }
}
