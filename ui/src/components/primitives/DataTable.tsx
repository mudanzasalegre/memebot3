import type { ReactNode } from "react";


export interface DataColumn<Row> {
  id: string;
  header: string;
  align?: "left" | "right";
  mono?: boolean;
  render: (row: Row) => ReactNode;
}

interface DataTableProps<Row> {
  columns: DataColumn<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string;
  emptyMessage?: string;
  density?: "compact" | "comfortable";
}


export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  emptyMessage = "No rows",
  density = "compact",
}: DataTableProps<Row>) {
  return (
    <div className="data-table__wrap">
      <table className={`data-table data-table--${density}`}>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.id} className={`is-${column.align || "left"}`}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length > 0 ? (
            rows.map((row) => (
              <tr key={rowKey(row)}>
                {columns.map((column) => (
                  <td
                    key={column.id}
                    className={[
                      `is-${column.align || "left"}`,
                      column.mono ? "is-mono" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            ))
          ) : (
            <tr>
              <td className="data-table__empty" colSpan={columns.length}>
                {emptyMessage}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
