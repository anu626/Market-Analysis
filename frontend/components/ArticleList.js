import ArticleCard from "./ArticleCard";

export default function ArticleList({ articles }) {
  if (!articles || articles.length === 0) {
    return <div className="empty">No articles yet. Trigger ingestion to fetch some.</div>;
  }
  return (
    <ol style={{ listStyle: "none", padding: 0, margin: 0 }}>
      {articles.map((a, i) => (
        <ArticleCard key={a.id} article={a} index={i + 1} />
      ))}
    </ol>
  );
}
