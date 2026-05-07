import { useCallback, useEffect, useState } from "react";
import ArticleList from "../components/ArticleList";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function Home() {
  const [articles, setArticles] = useState([]);
  const [view, setView] = useState("ranked"); // 'ranked' | 'latest'
  const [loading, setLoading] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [error, setError] = useState(null);
  const [status, setStatus] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const path = view === "ranked" ? "/articles" : "/articles/latest";
      const resp = await fetch(`${API_URL}${path}?limit=50`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setArticles(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [view]);

  useEffect(() => {
    load();
  }, [load]);

  const triggerIngest = async () => {
    setIngesting(true);
    setStatus("Ingesting...");
    try {
      const resp = await fetch(`${API_URL}/ingest`, { method: "POST" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const stats = await resp.json();
      setStatus(
        `Inserted ${stats.inserted}, dupes ${stats.duplicates}, errors ${stats.errors}`
      );
      await load();
    } catch (e) {
      setStatus(`Failed: ${e.message}`);
    } finally {
      setIngesting(false);
    }
  };

  return (
    <>
      <header className="header">
        <h1>Tech News Aggregator</h1>
        <nav>
          <button
            aria-pressed={view === "ranked"}
            onClick={() => setView("ranked")}
          >
            Ranked
          </button>
          <button
            aria-pressed={view === "latest"}
            onClick={() => setView("latest")}
          >
            Latest
          </button>
        </nav>
      </header>

      <main className="container">
        <div className="refresh">
          <button onClick={load} disabled={loading}>
            {loading ? "Loading..." : "Refresh"}
          </button>
          <button onClick={triggerIngest} disabled={ingesting}>
            {ingesting ? "Ingesting..." : "Trigger Ingestion"}
          </button>
          <span className="status">{status}</span>
        </div>

        {error && <div className="error">Error: {error}</div>}
        {!error && <ArticleList articles={articles} />}
      </main>
    </>
  );
}
