/* global XLSX, Plotly */
(function () {
  function toCSV(rows, columns) {
    const header = columns.join(',');
    const lines = rows.map(r => columns.map(c => {
      const v = r[c] ?? '';
      const s = (typeof v === 'string') ? v.replace(/"/g, '""') : v;
      return `"${s}"`;
    }).join(','));
    return [header].concat(lines).join('\n');
  }

  function arrayToRows(labels, values, labelKey='Label', valueKey='Value') {
    const out = [];
    const n = Math.max(labels?.length || 0, values?.length || 0);
    for (let i=0;i<n;i++) {
      out.push({ [labelKey]: labels?.[i] ?? '', [valueKey]: values?.[i] ?? '' });
    }
    return out;
  }

  async function exportArrayToCSV(filename, labels, values, labelKey='Label', valueKey='Value') {
    const rows = arrayToRows(labels, values, labelKey, valueKey);
    const csv = toCSV(rows, [labelKey, valueKey]);
    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename.endsWith('.csv') ? filename : (filename + '.csv');
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function exportArrayToXLSX(filename, labels, values, labelKey='Label', valueKey='Value') {
    if (typeof XLSX === 'undefined') {
      alert('Excel export library not loaded.');
      return;
    }
    const rows = arrayToRows(labels, values, labelKey, valueKey);
    const ws = XLSX.utils.json_to_sheet(rows);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, 'Data');
    const wbout = XLSX.write(wb, {bookType:'xlsx', type:'array'});
    const blob = new Blob([wbout], {type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename.endsWith('.xlsx') ? filename : (filename + '.xlsx');
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function exportTableToCSV(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) return;
    const rows = Array.from(table.querySelectorAll('tr'));
    const csv = rows.map(row =>
      Array.from(row.querySelectorAll('th,td'))
        .map(td => `"${td.textContent.trim().replace(/"/g,'""')}"`).join(',')
    ).join('\n');
    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename.endsWith('.csv') ? filename : (filename + '.csv');
    a.click();
    URL.revokeObjectURL(a.href);
  }

  window.ExportKit = {
    exportArrayToCSV,
    exportArrayToXLSX,
    exportTableToCSV,
  };
})();

