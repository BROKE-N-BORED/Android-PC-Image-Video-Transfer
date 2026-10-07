using AndroidPhotoTransfer.Core.Media;

namespace AndroidPhotoTransfer.Core.Transfer
{
    public readonly record struct TransferSnapshot(
        string CurrentFile,
        long CurrentFileBytes,
        long CurrentFileSize,
        int FilesDone,
        int FilesTotal,
        long BytesProcessed,
        long BytesTotal,
        long BytesCopied,
        int Copied,
        int Skipped,
        int Failed);

    /// <summary>
    /// Written by the transfer engine on every chunk, read by the UI on a 250-500 ms timer,
    /// so the progress display is throttled without slowing the copy loop.
    /// </summary>
    public sealed class TransferProgress
    {
        private readonly object _gate = new();
        private string _currentFile = "";
        private long _currentFileBytes;
        private long _currentFileSize;
        private int _filesDone;
        private int _filesTotal;
        private long _bytesProcessed;
        private long _bytesTotal;
        private long _bytesCopied;
        private int _copied;
        private int _skipped;
        private int _failed;

        public void Begin(int files, long bytes)
        {
            lock (_gate)
            {
                _filesTotal = files;
                _bytesTotal = bytes;
                _filesDone = 0;
                _bytesProcessed = 0;
                _bytesCopied = 0;
                _copied = _skipped = _failed = 0;
                _currentFile = "";
                _currentFileBytes = _currentFileSize = 0;
            }
        }

        public void BeginFile(MediaItem item)
        {
            lock (_gate)
            {
                _currentFile = item.Name;
                _currentFileSize = item.Size;
                _currentFileBytes = 0;
            }
        }

        public void AddBytes(int count)
        {
            lock (_gate)
            {
                _currentFileBytes += count;
                _bytesProcessed += count;
                _bytesCopied += count;
            }
        }

        /// <summary>Forget the bytes of a failed attempt before retrying the same file.</summary>
        public void RollbackFile()
        {
            lock (_gate)
            {
                _bytesProcessed -= _currentFileBytes;
                _bytesCopied -= _currentFileBytes;
                _currentFileBytes = 0;
            }
        }

        public void EndFile(TransferState state)
        {
            lock (_gate)
            {
                if (state is TransferState.Pending or TransferState.Transferring) return;

                // Count the whole file as processed so the overall bar reaches 100% even for skipped/failed files.
                _bytesProcessed += Math.Max(0, _currentFileSize - _currentFileBytes);
                _filesDone++;
                switch (state)
                {
                    case TransferState.Complete: _copied++; break;
                    case TransferState.Skipped: _skipped++; break;
                    case TransferState.Failed: _failed++; break;
                }
            }
        }

        /// <summary>Un-counts files that were marked failed but are going back into the queue.</summary>
        public void UndoFailedFiles(IEnumerable<long> sizes)
        {
            lock (_gate)
            {
                foreach (var size in sizes)
                {
                    _failed--;
                    _filesDone--;
                    _bytesProcessed -= size;
                }
            }
        }

        public TransferSnapshot Snapshot()
        {
            lock (_gate)
            {
                return new TransferSnapshot(_currentFile, _currentFileBytes, _currentFileSize, _filesDone, _filesTotal,
                    _bytesProcessed, _bytesTotal, _bytesCopied, _copied, _skipped, _failed);
            }
        }
    }

    /// <summary>
    /// Async pause/resume without busy-waiting. Pausing also cancels <see cref="Token"/>, so a file that is
    /// mid-copy is closed immediately (the phone isn't left holding an open transfer) and restarted on resume.
    /// </summary>
    public sealed class PauseGate
    {
        private readonly object _gate = new();
        private TaskCompletionSource? _paused;
        private CancellationTokenSource _pauseCts = new();

        public bool IsPaused
        {
            get { lock (_gate) return _paused != null; }
        }

        /// <summary>Cancelled the moment the user pauses.</summary>
        public CancellationToken Token
        {
            get { lock (_gate) return _pauseCts.Token; }
        }

        public void Pause()
        {
            CancellationTokenSource? toCancel = null;
            lock (_gate)
            {
                if (_paused != null) return;
                _paused = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                toCancel = _pauseCts;
            }
            toCancel.Cancel();
        }

        public void Resume()
        {
            TaskCompletionSource? paused;
            lock (_gate)
            {
                paused = _paused;
                _paused = null;
                if (_pauseCts.IsCancellationRequested)
                {
                    _pauseCts.Dispose();
                    _pauseCts = new CancellationTokenSource();
                }
            }
            paused?.TrySetResult();
        }

        public Task WaitAsync(CancellationToken ct)
        {
            Task? wait;
            lock (_gate) wait = _paused?.Task;
            return wait == null ? Task.CompletedTask : wait.WaitAsync(ct);
        }
    }
}
