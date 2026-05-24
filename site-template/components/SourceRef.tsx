export function SourceRef({ id, quote }: { id: string; quote?: string }) {
  return (
    <span className="source-ref" title={quote}>
      {id}
    </span>
  );
}
