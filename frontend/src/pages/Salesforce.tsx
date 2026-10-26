import { useCallback, useEffect, useState } from "react";
import { ApiError, rest, salesforceApi } from "../api";
import type { SalesforceOpportunity } from "../api";

interface SalesforceAccount {
  Id: string;
  Name: string;
  AccountNumber?: string | null;
  Rating?: string | null;
}

export function SalesforcePage() {
  const [query, setQuery] = useState("");
  const [accounts, setAccounts] = useState<SalesforceAccount[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [searched, setSearched] = useState(false);

  const [expandedAccount, setExpandedAccount] = useState<string | null>(null);
  const [opportunities, setOpportunities] = useState<
    Record<string, SalesforceOpportunity[]>
  >({});
  const [opportunityLoading, setOpportunityLoading] = useState<
    Record<string, boolean>
  >({});
  const [opportunityErrors, setOpportunityErrors] = useState<
    Record<string, string>
  >({});

  const searchAccounts = useCallback(async (term: string) => {
    setLoading(true);
    setError("");
    setSearched(true);
    setExpandedAccount(null);

    try {
      const params = new URLSearchParams();
      if (term.trim()) params.set("query", term.trim());

      const suffix = params.toString();
      const result = await rest<SalesforceAccount[]>(
        `/api/salesforce/accounts${suffix ? `?${suffix}` : ""}`
      );

      setAccounts(result);
    } catch (err) {
      setAccounts([]);
      setError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Unable to load Salesforce accounts."
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void searchAccounts("");
  }, [searchAccounts]);

  async function toggleOpportunities(accountId: string) {
    if (expandedAccount === accountId) {
      setExpandedAccount(null);
      return;
    }

    setExpandedAccount(accountId);

    if (opportunities[accountId] !== undefined) return;

    setOpportunityLoading((current) => ({ ...current, [accountId]: true }));
    setOpportunityErrors((current) => ({ ...current, [accountId]: "" }));

    try {
      const result = await salesforceApi.opportunities(accountId);
      setOpportunities((current) => ({ ...current, [accountId]: result }));
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Unable to load opportunities.";

      setOpportunityErrors((current) => ({
        ...current,
        [accountId]: message,
      }));
    } finally {
      setOpportunityLoading((current) => ({
        ...current,
        [accountId]: false,
      }));
    }
  }

  async function retryOpportunities(accountId: string) {
    setOpportunities((current) => {
      const next = { ...current };
      delete next[accountId];
      return next;
    });
    setOpportunityErrors((current) => ({ ...current, [accountId]: "" }));
    setOpportunityLoading((current) => ({ ...current, [accountId]: true }));

    try {
      const result = await salesforceApi.opportunities(accountId);
      setOpportunities((current) => ({ ...current, [accountId]: result }));
    } catch (err) {
      setOpportunityErrors((current) => ({
        ...current,
        [accountId]:
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : "Unable to load opportunities.",
      }));
    } finally {
      setOpportunityLoading((current) => ({
        ...current,
        [accountId]: false,
      }));
    }
  }

  function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    void searchAccounts(query);
  }

  function formatAmount(amount: number | null | undefined) {
    if (amount == null) return "—";
    return new Intl.NumberFormat("en-CA", {
      style: "currency",
      currency: "CAD",
      maximumFractionDigits: 2,
    }).format(amount);
  }

  return (
    <>
      <div className="page-head">
        <div>
          <p className="eyebrow">CRM integration</p>
          <h1>Salesforce accounts</h1>
          <p className="lede">
            Search customer accounts from your connected Salesforce
            organization. Results are retrieved through the AeroNexus backend.
          </p>
        </div>
        <button
          className="btn"
          type="button"
          onClick={() => void searchAccounts(query)}
          disabled={loading}
        >
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2>Account directory</h2>
          <span className="small muted">
            {searched && !loading
              ? `${accounts.length} account${accounts.length === 1 ? "" : "s"}`
              : "Up to 20 results"}
          </span>
        </div>

        <form onSubmit={handleSubmit} className="row">
          <input
            aria-label="Search Salesforce accounts"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by account name..."
            style={{ flex: "1 1 260px", minWidth: 0 }}
          />
          <button className="btn primary" type="submit" disabled={loading}>
            {loading ? "Searching..." : "Search accounts"}
          </button>
        </form>
        <p className="small muted">
          Search is read-only. No Salesforce records are changed.
        </p>
      </div>

      {error && (
        <div className="err section" role="alert">
          <strong>Salesforce request failed</strong>
          <p>{error}</p>
          <p className="small">
            Check that the backend is running, the Salesforce integration is
            enabled, and your current demo role has read permission.
          </p>
          <button
            className="btn"
            type="button"
            onClick={() => void searchAccounts(query)}
            disabled={loading}
          >
            Try again
          </button>
        </div>
      )}

      {loading && (
        <div className="panel section" aria-live="polite">
          Loading Salesforce accounts...
        </div>
      )}

      {!loading && !error && searched && accounts.length === 0 && (
        <div className="panel section">
          <h2>No accounts found</h2>
          <p className="muted">
            Try another account name or clear the search field to view all
            available accounts.
          </p>
        </div>
      )}

      {!loading && !error && accounts.length > 0 && (
        <div className="panel flush section">
          <table>
            <thead>
              <tr>
                <th>Account name</th>
                <th>Account number</th>
                <th>Rating</th>
                <th>Salesforce ID</th>
                <th>Related records</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((account) => {
                const isExpanded = expandedAccount === account.Id;
                const isLoadingOpportunities =
                  opportunityLoading[account.Id] ?? false;
                const opportunityError = opportunityErrors[account.Id];
                const accountOpportunities = opportunities[account.Id];

                return (
                  <>
                    <tr key={account.Id}>
                      <td>
                        <strong>{account.Name}</strong>
                      </td>
                      <td>{account.AccountNumber || "—"}</td>
                      <td>{account.Rating || "Not rated"}</td>
                      <td className="mono">{account.Id}</td>
                      <td>
                        <button
                          className="btn"
                          type="button"
                          aria-expanded={isExpanded}
                          onClick={() =>
                            void toggleOpportunities(account.Id)
                          }
                        >
                          {isExpanded
                            ? "Hide opportunities"
                            : "View opportunities"}
                        </button>
                      </td>
                    </tr>

                    {isExpanded && (
                      <tr key={`${account.Id}-opportunities`}>
                        <td colSpan={5}>
                          <section
                            aria-label={`Opportunities for ${account.Name}`}
                          >
                            <div className="panel-head">
                              <h3>Opportunities — {account.Name}</h3>
                              {accountOpportunities !== undefined && (
                                <span className="small muted">
                                  {accountOpportunities.length}{" "}
                                  {accountOpportunities.length === 1
                                    ? "opportunity"
                                    : "opportunities"}
                                </span>
                              )}
                            </div>

                            {isLoadingOpportunities && (
                              <p className="small muted" aria-live="polite">
                                Loading Salesforce opportunities...
                              </p>
                            )}

                            {!isLoadingOpportunities && opportunityError && (
                              <div className="err" role="alert">
                                <p>{opportunityError}</p>
                                <button
                                  className="btn"
                                  type="button"
                                  onClick={() =>
                                    void retryOpportunities(account.Id)
                                  }
                                >
                                  Try again
                                </button>
                              </div>
                            )}

                            {!isLoadingOpportunities &&
                              !opportunityError &&
                              accountOpportunities !== undefined &&
                              accountOpportunities.length === 0 && (
                                <p className="small muted">
                                  No opportunities were returned for this
                                  account.
                                </p>
                              )}

                            {!isLoadingOpportunities &&
                              !opportunityError &&
                              accountOpportunities !== undefined &&
                              accountOpportunities.length > 0 && (
                                <div style={{ overflowX: "auto" }}>
                                  <table>
                                    <thead>
                                      <tr>
                                        <th>Opportunity</th>
                                        <th>Stage</th>
                                        <th>Amount</th>
                                        <th>Close date</th>
                                        <th>Probability</th>
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {accountOpportunities.map((opportunity) => (
                                        <tr key={opportunity.Id}>
                                          <td>
                                            <strong>{opportunity.Name}</strong>
                                          </td>
                                          <td>{opportunity.StageName || "—"}</td>
                                          <td>
                                            {formatAmount(opportunity.Amount)}
                                          </td>
                                          <td>
                                            {opportunity.CloseDate || "—"}
                                          </td>
                                          <td>
                                            {opportunity.Probability == null
                                              ? "—"
                                              : `${opportunity.Probability}%`}
                                          </td>
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                </div>
                              )}

                            <p className="small muted">
                              Opportunity data is read-only. Amounts are
                              displayed in CAD; confirm the Salesforce
                              organization's currency if it uses another
                              currency.
                            </p>
                          </section>
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="small muted section">
        Account data is supplied by the configured Salesforce connector. The
        table displays only the fields returned by the backend.
      </p>
    </>
  );
}
