import { gql, Q } from "../api";
import { label } from "../lib/cloud";
import { useAsync } from "../lib/useAsync";

/* eslint-disable @typescript-eslint/no-explicit-any */
export function DatasetsPage() {
  const { data, error } = useAsync(() => gql<{ datasets: any[]; rules: any[] }>(Q.datasets));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Data and rules</h1>
          <p className="lede">Where every training set comes from, what its license allows, and every versioned rule the calculations use.</p>
        </div>
      </div>
      {error && <div className="err">{error}</div>}
      <div className="panel flush">
        <table>
          <thead><tr><th>Dataset</th><th>License and terms</th><th>Used for</th><th>Here</th></tr></thead>
          <tbody>{(data?.datasets ?? []).map((d) => (
            <tr key={d.key}>
              <td><a href={d.source} target="_blank" rel="noreferrer">{d.name}</a><div className="src">{d.publisher}</div></td>
              <td className="small">{d.license}<div className="src">{d.terms}</div></td>
              <td className="small">{d.used_for}</td>
              <td><span className={`status ${d.available ? "auto_accepted" : d.auto_fetch ? "needs_review" : "missing"}`}>
                {d.available ? "Downloaded" : d.auto_fetch ? "Run fetch script" : "Manual export"}</span></td>
            </tr>))}</tbody>
        </table>
      </div>
      <p className="small muted">Fetch: <code>python scripts/fetch_public_data.py</code> (checksums verified). Train: <code>python scripts/train_public_models.py</code>.</p>
      <h2 className="section">Rules in force</h2>
      <div className="panel flush">
        <table>
          <thead><tr><th>Rule</th><th>Version</th><th>Effective</th><th>Parameters</th><th>Why</th></tr></thead>
          <tbody>{(data?.rules ?? []).map((r) => (
            <tr key={`${r.id}-${r.version}`}><td>{label(r.id.replace(".", ": "))}</td><td>v{r.version}</td><td>{r.effective_from}</td>
              <td className="small">{JSON.stringify(r.params)}</td><td className="small muted">{r.rationale}</td></tr>))}</tbody>
        </table>
      </div>
    </>
  );
}
