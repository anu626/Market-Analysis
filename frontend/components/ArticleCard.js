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
  return (
    <li className="article">
      <div className="article-title">
        {index !== undefined && <span style={{ color: "#828282", marginRight: 6 }}>{index}.</span>}
        <a href={article.url} target="_blank" rel="noopener noreferrer">
          {article.title}
        </a>
        {domain && <span className="article-meta domain"> ({domain})</span>}
      </div>
      <div className="article-meta">
        {article.score} points
        <span className="source">{article.source_name}</span>
        <span style={{ marginLeft: 6 }}>{timeAgo(article.published_at || article.created_at)}</span>
        <span style={{ marginLeft: 6, color: "#aaa" }}>rank {article.rank_score?.toFixed(2)}</span>
      </div>
    </li>
  );
}
