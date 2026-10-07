using AndroidPhotoTransfer.Core.Media;

namespace AndroidPhotoTransfer.Data
{
    /// <summary>Remembers which phone files were already copied (metadata only), for "Import New".</summary>
    public interface IHistoryDatabase
    {
        void Record(string deviceKey, MediaItem item, string destination);

        /// <summary>History keys (<see cref="MediaItem.HistoryKey"/>) of everything already transferred from this phone.</summary>
        HashSet<string> GetTransferredKeys(string deviceKey);
    }
}
