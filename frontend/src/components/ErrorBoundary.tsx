import { Component, type ReactNode } from "react";

/** Keeps one page's rendering error from blanking the whole app; shows what failed and a way back. */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidUpdate(prev: { resetKey?: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }
  render() {
    if (this.state.error) {
      return (
        <div className="panel" role="alert">
          <h2>This page hit an error</h2>
          <p className="small muted">{this.state.error.message}</p>
          <button className="btn" onClick={() => this.setState({ error: null })}>Try again</button>
        </div>
      );
    }
    return this.props.children;
  }
}
