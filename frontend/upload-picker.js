// The drop target and native file picker share one validation/change path.
export function initUploadPicker({ zone, input, status, onChange, onError, isBusy = () => false }) {
  const prompt = 'Drop one CSV or PDF here, or click to choose a file';
  let dragDepth = 0;
  const clearDrag = () => { dragDepth = 0; zone.classList.remove('drag-over'); };
  const isFileDrag = event => Array.from(event.dataTransfer?.types || []).includes('Files');

  function select(files, fromDrop = false) {
    const file = files?.[0];
    let error = '';
    if (files?.length > 1) error = 'Choose one statement at a time.';
    else if (file && !/\.(csv|pdf)$/i.test(file.name)) error = 'Please choose a CSV or PDF statement.';
    else if (file && file.size === 0) error = 'That file is empty. Choose a statement containing transactions.';
    else if (file && file.size > 10 * 1024 * 1024) error = 'Choose a statement no larger than 10 MB.';

    if (error || !file) input.value = '';
    else if (fromDrop) input.files = files;
    onChange(); // Always invalidate an earlier preview/replacement confirmation.
    onError(error);
    status.textContent = !error && file ? `${file.name} selected — review the statement below` : prompt;
  }

  status.textContent = prompt;
  zone.addEventListener('click', () => { if (!isBusy()) input.click(); });
  input.addEventListener('change', () => select(input.files));
  zone.addEventListener('dragenter', event => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    if (!isBusy()) { dragDepth++; zone.classList.add('drag-over'); }
  });
  zone.addEventListener('dragover', event => {
    if (!isFileDrag(event)) return;
    event.preventDefault(); // Required: without this the browser will not deliver a usable drop.
    event.dataTransfer.dropEffect = isBusy() ? 'none' : 'copy';
  });
  zone.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) zone.classList.remove('drag-over');
  });
  zone.addEventListener('drop', event => {
    if (!isFileDrag(event) && !event.dataTransfer?.files?.length) return;
    event.preventDefault();
    clearDrag();
    if (!isBusy()) select(event.dataTransfer.files, true);
  });
  zone.addEventListener('dragend', clearDrag);
}
