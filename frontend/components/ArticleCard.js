function domainOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const diffSec = Math.max(1, Math.floor((Date.now() - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  const m = Math.floor(diffSec / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export default function ArticleCard({ article, index }) {
  const domain = domainOf(article.url);
  const highlighted = article.is_highlighted;
  const displayTitle = article.ai_title || article.title;
  const displaySummary = article.ai_summary || article.summary;
  const aiEnriched = !!article.ai_title;

  return (
    <li className="article">
      <div className="article-title">
        {index !== undefined && <span style={{ color: "#828282", marginRight: 6 }}>{index}.</span>}
        <a href={article.url} target="_blank" rel="noopener noreferrer">
          {displayTitle}
        </a>
        {domain && <span className="article-meta domain"> ({domain})</span>}
      </div>
      {displaySummary && (
        <div style={{
          fontSize: 13, color: "#444", marginTop: 4, lineHeight: 1.5,
          display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden",
        }}>
          {displaySummary}
          {aiEnriched && (
            <span style={{
              marginLeft: 6, fontSize: 10, color: "#9ca3af",
              fontStyle: "italic", verticalAlign: "middle",
            }}>✦ AI</span>
          )}
        </div>
      )}
      <div className="article-meta">
        {article.vertical && (
          <span style={{
            display: "inline-block", padding: "2px 8px", borderRadius: 4,
            backgroundColor: "#1a1a2e", color: "#e2a84b", fontSize: 11,
            fontWeight: 600, marginRight: 8, letterSpacing: 0.3,
          }}>
            {article.vertical}
          </span>
        )}
        <span className="source">{article.source_name}</span>
        <span style={{ marginLeft: 6 }}>{timeAgo(article.published_at || article.created_at)}</span>
        <span style={{ marginLeft: 6, color: "#aaa" }}>rank {article.rank_score?.toFixed(2)}</span>
      </div>
    </li>
  );
}
