import { useEffect, useState } from "react";
import { getAdminFeedback, getAdminOverview, getAdminRequests } from "./api";
import type { AdminFeedbackRow, AdminOverview, AdminRequestRow } from "./types";

const SQL_PREVIEW_LEN = 80;

function truncate(text: string | null, max: number): string {
  if (!text) return "";
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function formatPct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

export default function Admin() {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [requests, setRequests] = useState<AdminRequestRow[] | null>(null);
  const [feedback, setFeedback] = useState<AdminFeedbackRow[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    Promise.all([getAdminOverview(), getAdminRequests(), getAdminFeedback()])
      .then(([ov, req, fb]) => {
        if (cancelled) return;
        setOverview(ov);
        setRequests(req);
        setFeedback(fb);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(`Couldn't load admin data: ${err}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <div className="admin-status">Loading…</div>;
  if (error) return <div className="admin-status admin-error">{error}</div>;
  if (!overview || !requests || !feedback) return null;

  return (
    <div className="admin">
      <section className="admin-tiles">
        <div className="tile">
          <span className="tile-value">{overview.total_requests}</span>
          <span className="tile-label">Total requests</span>
        </div>
        <div className="tile">
          <span className="tile-value">{formatPct(overview.error_rate)}</span>
          <span className="tile-label">Error rate</span>
        </div>
        <div className="tile">
          <span className="tile-value">{overview.p50_latency_ms}ms</span>
          <span className="tile-label">p50 latency</span>
        </div>
        <div className="tile">
          <span className="tile-value">
            {overview.feedback_up}👍 / {overview.feedback_down}👎
          </span>
          <span className="tile-label">Feedback</span>
        </div>
      </section>

      {Object.keys(overview.by_intent).length > 0 && (
        <section className="admin-section">
          <h2>By intent</h2>
          <ul className="admin-by-intent">
            {Object.entries(overview.by_intent).map(([intent, count]) => (
              <li key={intent}>
                <span>{intent}</span>
                <span>{count}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="admin-section">
        <h2>Recent requests</h2>
        {requests.length === 0
          ? <div className="admin-empty">No requests logged yet.</div>
          : (
            <div className="tablewrap">
              <table>
                <thead>
                  <tr>
                    <th>Time</th><th>Role</th><th>Intent</th><th>Status</th>
                    <th>Latency</th><th>SQL</th>
                  </tr>
                </thead>
                <tbody>
                  {requests.map((r) => (
                    <tr key={r.request_id ?? `${r.created_at}-${r.question}`}>
                      <td>{r.created_at}</td>
                      <td>{r.user_role}</td>
                      <td>{r.intent}</td>
                      <td>{r.status}{r.error_type ? ` (${r.error_type})` : ""}</td>
                      <td>{r.e2e_ms != null ? `${r.e2e_ms}ms` : ""}</td>
                      <td><code>{truncate(r.sql_text, SQL_PREVIEW_LEN)}</code></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
      </section>

      <section className="admin-section">
        <h2>Feedback</h2>
        {feedback.length === 0
          ? <div className="admin-empty">No feedback recorded yet.</div>
          : (
            <ul className="admin-feedback-list">
              {feedback.map((f) => (
                <li key={f.feedback_id ?? `${f.created_at}-${f.request_id}`}>
                  <span className={`fb-rating ${f.rating}`}>
                    {f.rating === "up" ? "👍" : "👎"}
                  </span>
                  <span className="admin-feedback-comment">{f.comment || "(no comment)"}</span>
                  <span className="admin-feedback-time">{f.created_at}</span>
                </li>
              ))}
            </ul>
          )}
      </section>
    </div>
  );
}
