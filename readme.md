# Supernote `.note` to PDF Converter

Convert a Supernote `.note` file into a more useful PDF.

The PDF can include:

* rendered notebook pages
* searchable handwriting/text
* bookmarks from Supernote headings
* keywords in the PDF outline
* starred pages
* internal note links converted into clickable PDF links

---

## Basic Example

To convert a Supernote file to PDF:

```bash
python note_to_pdf.py mynotebook.note
```

This creates a PDF in an `output` folder beside the original `.note` file.

For example:

```text
mynotebook.note
output/mynotebook.pdf
```

That is the normal use case.

---

## What This Script Does

This script takes a Supernote `.note` file and creates an enhanced PDF version.

It tries to preserve the useful structure from the notebook, including headings, keywords, stars, links, and searchable text.

This makes the exported PDF easier to:

* search
* archive
* navigate
* open on other devices
* use in document systems
* use with AI/RAG tools

---

## Installation

Install the required Python packages:

```bash
pip install supernotelib pypdf reportlab pillow
```

---

## Basic Usage

Run:

```bash
python note_to_pdf.py mynotebook.note
```

The script will:

1. Load the Supernote notebook.
2. Render the pages as a PDF.
3. Extract handwriting recognition text where available.
4. Extract editable textbox text.
5. Add searchable text to the PDF.
6. Add PDF bookmarks from headings and keywords.
7. Preserve internal Supernote links where possible.
8. Save the final PDF.

---

## Example Output

If your input file is:

```text
Journal.note
```

The output will usually be:

```text
output/Journal.pdf
```

The script also prints a summary like:

```text
✓ PDF    → output/Journal.pdf
✓ Total  : 2m 14.8s
  Searchable text : 84/120 pages
  Bookmarks       : 32 heading(s), 5 starred
  Internal links  : 18 tap targets
```

---

## What Gets Added to the PDF

### Searchable Text

The script extracts recognised handwriting and textbox text, then adds it to the PDF as invisible searchable text.

This means you can search the PDF in a normal PDF reader.

---

### Bookmarks

Supernote headings are added as PDF bookmarks.

This makes large notebooks much easier to navigate.

---

### Keywords

Supernote keywords are added to the PDF outline.

This helps you find important pages quickly.

---

### Starred Pages

Starred pages are marked with a star symbol:

```text
★
```

---

### Internal Links

If your Supernote notebook has internal page links, the script attempts to recreate them as clickable links in the PDF.

---

## Advanced Options

Most of the time, you only need the basic command:

```bash
python note_to_pdf.py mynotebook.note
```

The options below are for checking, debugging, searching, or reducing file size.

---

### Print Table of Contents Only

Use this to check headings, stars, and keywords without creating a PDF.

```bash
python note_to_pdf.py mynotebook.note --toc
```

---

### Search Textboxes

Search editable textboxes inside the `.note` file.

```bash
python note_to_pdf.py mynotebook.note --search "meeting notes"
```

Search for multiple terms:

```bash
python note_to_pdf.py mynotebook.note --search project recovery
```

---

### Test One Page Only

Process one page without writing a PDF.

```bash
python note_to_pdf.py mynotebook.note --page 5
```

This is useful for testing large notebooks.

---

### Change Number of Render Workers

By default, the script uses 4 workers.

```bash
python note_to_pdf.py mynotebook.note --workers 2
```

Use fewer workers if the conversion crashes or uses too much memory.

---

### Compress the PDF

Use JPEG compression to reduce the PDF file size.

```bash
python note_to_pdf.py mynotebook.note --quality 60
```

The value should normally be between `1` and `95`.

Lower numbers produce smaller files but lower image quality.

---

### Choose Output Folder

Save the PDF somewhere else.

```bash
python note_to_pdf.py mynotebook.note --out ./converted
```

---

## Advanced Flag Summary

| Option             | What it does                                                |
| ------------------ | ----------------------------------------------------------- |
| `--toc`            | Prints headings, stars, and keywords without creating a PDF |
| `--search TERM...` | Searches editable textboxes                                 |
| `--page N`         | Tests one page only and skips PDF output                    |
| `--workers N`      | Sets the number of PDF render workers                       |
| `--quality N`      | Compresses PDF images using JPEG quality                    |
| `--out FOLDER`     | Sets the output folder                                      |

---

## Troubleshooting

### Missing Packages

Install the required packages:

```bash
pip install supernotelib pypdf reportlab pillow
```

---

### The PDF Is Too Large

Try compression:

```bash
python note_to_pdf.py mynotebook.note --quality 60
```

---

### Conversion Crashes

Try reducing workers:

```bash
python note_to_pdf.py mynotebook.note --workers 1
```

---

### No PDF Is Created

The `.note` file may be from a newer Supernote format that `supernotelib` cannot render yet.

You may still be able to inspect headings and keywords:

```bash
python note_to_pdf.py mynotebook.note --toc
```

---

### Some Headings Appear as `Heading 1`, `Heading 2`, etc.

This means the script found a heading marker but could not confidently work out the heading text.

This can happen if:

* handwriting recognition was unavailable
* the heading text was not recognised
* the heading was outside the expected area
* the Supernote file format stored the data differently

---

## Why Use This?

Supernote notebooks are useful on the device, but exporting them as searchable, navigable PDFs makes them easier to reuse elsewhere.

This script is useful for:

* journals
* project notes
* meeting notes
* study notes
* long notebooks
* notebooks with headings and keywords
* notebooks with internal links
* personal knowledge bases
* AI/RAG document pipelines

---

## Notes

This script partly relies on Supernote’s internal `.note` file structure.

If Supernote changes the file format, some features may need updating.

The normal conversion should still be tried first:

```bash
python note_to_pdf.py mynotebook.note
```

---

## License

This project is licensed under the MIT License.

```text
MIT License

Copyright (c) 2026 Clayton Edge

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

You can also create a separate file called `LICENSE` containing the same MIT License text.
