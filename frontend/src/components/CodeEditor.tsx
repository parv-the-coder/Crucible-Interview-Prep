import Editor from "@monaco-editor/react";

const MONACO_LANGUAGE: Record<string, string> = {
  python: "python",
  javascript: "javascript",
  cpp: "cpp",
  java: "java",
  go: "go",
  sql: "sql",
};

interface Props {
  value: string;
  language: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
  height?: string;
}

export function CodeEditor({ value, language, onChange, readOnly, height = "100%" }: Props) {
  return (
    <Editor
      height={height}
      theme="vs-dark"
      language={MONACO_LANGUAGE[language] ?? "plaintext"}
      value={value}
      onChange={(next) => onChange(next ?? "")}
      loading={<div className="editor-loading">Loading editor…</div>}
      options={{
        readOnly,
        fontSize: 14,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        automaticLayout: true,
        tabSize: 4,
        renderWhitespace: "selection",
        // The candidate is writing an interview answer, not shipping code.
        // Autocomplete popups get in the way more than they help.
        quickSuggestions: false,
        suggestOnTriggerCharacters: false,
      }}
    />
  );
}
