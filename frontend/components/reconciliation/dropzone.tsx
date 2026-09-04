"use client";

import clsx from "clsx";
import { useRef, useState, type DragEvent } from "react";

interface DropzoneProps {
  accept: string;
  label: string;
  hint: string;
  file: File | null;
  busy?: boolean;
  onFile: (file: File | null) => void;
}

/** Click-to-browse or drag-and-drop file zone (Blade styling). */
export function Dropzone({
  accept,
  label,
  hint,
  file,
  busy,
  onFile,
}: DropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    const dropped = event.dataTransfer.files?.[0];
    if (dropped) {
      onFile(dropped);
    }
  }

  return (
    <div className="rounded-lg border border-line bg-white p-4">
      <p className="text-sm font-medium text-navy">{label}</p>
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        className="hidden"
        onChange={(e) => onFile(e.target.files?.[0] ?? null)}
      />
      <div
        role="button"
        tabIndex={0}
        onClick={() => !busy && inputRef.current?.click()}
        onKeyDown={(e) => {
          if ((e.key === "Enter" || e.key === " ") && !busy) {
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        className={clsx(
          "mt-3 flex cursor-pointer flex-col items-center justify-center rounded-md border border-dashed px-4 py-6 text-center transition-colors",
          dragging ? "border-primary bg-primary-50" : "border-line hover:border-primary/50",
          busy && "cursor-wait opacity-60"
        )}
      >
        {file ? (
          <>
            <p className="max-w-full truncate font-mono text-sm text-navy">{file.name}</p>
            <p className="mt-1 text-xs text-slate-400">
              {(file.size / 1024).toFixed(1)} KB · click to replace
            </p>
          </>
        ) : (
          <>
            <span className="text-xl text-primary">⇪</span>
            <p className="mt-1 text-sm text-slate-500">
              Drop file here, or <span className="text-primary">browse</span>
            </p>
          </>
        )}
      </div>
      <p className="mt-2 text-[11px] text-slate-400">{hint}</p>
    </div>
  );
}
