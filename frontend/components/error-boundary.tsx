"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  label?: string;
}

interface State {
  hasError: boolean;
  message: string;
}

/**
 * Graceful degradation: if a widget (telemetry poller, ledger table, ...)
 * throws, only its section collapses into a fallback card — the rest of the
 * dashboard keeps running.
 */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, message: "" };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error(`[ErrorBoundary:${this.props.label ?? "section"}]`, error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="rounded-md border border-danger-soft bg-danger-soft p-4 text-sm text-danger">
          <p className="font-medium">This section hit an unexpected error.</p>
          <p className="mt-1 opacity-80">{this.state.message}</p>
        </div>
      );
    }
    return this.props.children;
  }
}
