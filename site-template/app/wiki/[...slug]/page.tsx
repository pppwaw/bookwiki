export default function WikiPage({ params }: { params: { slug: string[] } }) {
  return (
    <main className="shell">
      <h1>{params.slug.join("/")}</h1>
      <p>Markdown rendering is provided by the generated vault in later milestones.</p>
    </main>
  );
}
