/** Downscale big phone photos before upload (faster over tunnels, under proxy limits). */
export async function prepareImage(file: File, maxDim = 1800, quality = 0.88): Promise<{ blob: Blob; name: string }> {
  try {
    if (!file.type.startsWith("image/") || file.type === "image/gif") return { blob: file, name: file.name };
    const bmp = await createImageBitmap(file, { imageOrientation: "from-image" } as ImageBitmapOptions);
    const scale = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
    if (scale === 1 && file.size < 2_500_000) {
      bmp.close?.();
      return { blob: file, name: file.name };
    }
    const w = Math.round(bmp.width * scale);
    const h = Math.round(bmp.height * scale);
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return { blob: file, name: file.name };
    ctx.drawImage(bmp, 0, 0, w, h);
    bmp.close?.();
    const blob: Blob | null = await new Promise((r) => canvas.toBlob(r, "image/jpeg", quality));
    if (!blob) return { blob: file, name: file.name };
    return { blob, name: file.name.replace(/\.[^.]+$/, "") + ".jpg" };
  } catch {
    return { blob: file, name: file.name };
  }
}
