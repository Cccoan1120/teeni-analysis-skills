import fs from "node:fs/promises";
import path from "node:path";

export async function reserveOutputPath(outputDir, baseName) {
  await fs.mkdir(outputDir, { recursive: true });
  for (let version = 1; ; version += 1) {
    const suffix = version === 1 ? "" : `_v${version}`;
    const candidate = path.join(outputDir, `${baseName}${suffix}.xlsx`);
    try {
      const reservation = await fs.open(candidate, "wx");
      await reservation.close();
      return candidate;
    } catch (error) {
      if (error?.code === "EEXIST") continue;
      throw error;
    }
  }
}

export async function finalizeRun({
  tempDir,
  outputPath,
  outputPaths,
  completed,
  primaryError,
  remove = fs.rm,
}) {
  let cleanupError;
  if (tempDir) {
    try {
      await remove(tempDir, { recursive: true, force: true });
    } catch (error) {
      cleanupError = error;
    }
  }
  const reservedOutputs = outputPaths ?? (outputPath ? [outputPath] : []);
  if (!completed) {
    for (const reservedOutput of reservedOutputs) {
      try {
        await remove(reservedOutput, { force: true });
      } catch (error) {
        cleanupError ??= error;
      }
    }
  }
  if (primaryError) throw primaryError;
  if (cleanupError) throw cleanupError;
}
