/** Hand the browser a file to save. Fetched with the session header like any other call, so the
 *  token never has to appear in an address. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The file name a response asks to be saved under. */
export function attachmentName(response: Response, fallback: string): string {
  return /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? fallback;
}
