"use client";

import clsx from "clsx";
import type { InputHTMLAttributes } from "react";

export function Input({
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={clsx(
        "h-10 w-full rounded-md border border-line bg-white px-3 text-sm text-slate-800",
        "placeholder:text-slate-400 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/25",
        className
      )}
      {...props}
    />
  );
}
