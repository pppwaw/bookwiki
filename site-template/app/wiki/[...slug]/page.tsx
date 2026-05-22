export default function WikiPage({ params }: { params: { slug: string[] } }) {
  return (
    <main className="shell">
      <h1>{params.slug.join("/")}</h1>
      <p>MDX rendering is provided by the generated Fumadocs content source.</p>
    </main>
  );
}
