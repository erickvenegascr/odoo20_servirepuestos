package cr.servirepuestos.gateway;

import com.google.gson.GsonBuilder;
import com.healthmarketscience.jackcess.*;
import com.healthmarketscience.jackcess.impl.ColumnImpl;
import com.healthmarketscience.jackcess.impl.DatabaseImpl;
import com.healthmarketscience.jackcess.impl.TableImpl;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.math.BigDecimal;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.temporal.TemporalAccessor;
import java.util.*;

/** General read-only Access surface. Never resolves a linked database. */
public final class AccessGateway {
    private AccessGateway() { }
    private static Map<String, Object> map(Object... pairs) {
        Map<String, Object> value = new LinkedHashMap<>();
        for (int i = 0; i < pairs.length; i += 2) { value.put((String) pairs[i], pairs[i + 1]); }
        return value;
    }
    private static boolean hidden(String name) {
        return name.toLowerCase(Locale.ROOT).startsWith("msys") || name.startsWith("~");
    }
    private static Table table(Database db, String name) throws IOException {
        if (hidden(name)) { throw new IOException("SYSTEM_TABLE_DISABLED"); }
        TableMetaData metadata = db.getTableMetaData(name);
        if (metadata == null) { throw new IOException("TABLE_NOT_FOUND"); }
        if (metadata.isSystem()) { throw new IOException("SYSTEM_TABLE_DISABLED"); }
        if (metadata.isLinked()) { throw new IOException("LINKED_TABLE_REQUIRES_ITS_SOURCE_ADAPTER"); }
        return db.getTable(name);
    }
    public static Map<String, Object> describe(Database db) throws IOException {
        List<Object> tables = new ArrayList<>();
        for (String name : db.getTableNames()) {
            if (hidden(name)) { continue; }
            TableMetaData metadata = db.getTableMetaData(name);
            if (metadata.isSystem()) { continue; }
            if (metadata.isLinked()) {
                tables.add(map("name", name, "linked", true, "link_type", metadata.getType().name(),
                    "source_table", metadata.getLinkedTableName())); continue;
            }
            Table table = table(db, name);
            List<Object> columns = new ArrayList<>();
            for (Column col : table.getColumns()) {
                columns.add(map("name", col.getName(), "type", col.getType().name(),
                    "auto_number", col.isAutoNumber(), "length", col.getLength(),
                    "precision", col.getPrecision(), "scale", col.getScale()));
            }
            List<Object> indexes = new ArrayList<>();
            for (Index index : table.getIndexes()) {
                List<Object> keys = new ArrayList<>();
                for (Index.Column key : index.getColumns()) {
                    keys.add(map("column", key.getName(), "ascending", key.isAscending()));
                }
                indexes.add(map("name", index.getName(), "primary", index.isPrimaryKey(),
                    "unique", index.isUnique(), "foreign", index.isForeignKey(), "columns", keys));
            }
            tables.add(map("name", name, "linked", false, "row_count", table.getRowCount(),
                "columns", columns, "indexes", indexes));
        }
        List<Object> relationships = new ArrayList<>();
        for (Relationship rel : db.getRelationships()) {
            if (hidden(rel.getFromTable().getName()) || hidden(rel.getToTable().getName())) { continue; }
            List<String> from = new ArrayList<>(), to = new ArrayList<>();
            for (Column col : rel.getFromColumns()) { from.add(col.getName()); }
            for (Column col : rel.getToColumns()) { to.add(col.getName()); }
            relationships.add(map("name", rel.getName(), "from_table", rel.getFromTable().getName(),
                "from_columns", from, "to_table", rel.getToTable().getName(), "to_columns", to));
        }
        return map("format", db.getFileFormat().name(), "tables", tables, "relationships", relationships);
    }
    public static Map<String, Object> read(Database db, String name, String projection, int offset, int limit) throws IOException {
        if (offset < 0 || offset > 10000 || limit < 1 || limit > 500) { throw new IOException("INVALID_PAGE"); }
        Table table = table(db, name);
        Set<String> fields = new LinkedHashSet<>(Arrays.asList(projection.split(",", -1)));
        if (fields.size() > 32 || fields.contains("")) { throw new IOException("INVALID_PROJECTION"); }
        for (String field : fields) {
            Column col = table.getColumn(field);
            if (col == null || col.getType() == DataType.OLE || col.getType() == DataType.BINARY
                    || col.getType() == DataType.COMPLEX_TYPE) { throw new IOException("UNSUPPORTED_FIELD"); }
        }
        List<Object> rows = new ArrayList<>();
        int outputBudget = 500000;
        Cursor cursor = table.getDefaultCursor();
        int skipped = cursor.moveNextRows(offset);
        while (rows.size() < limit && cursor.moveToNextRow()) {
            Map<String, Object> row = new LinkedHashMap<>();
            for (Map.Entry<String, Object> cell : cursor.getCurrentRow(fields).entrySet()) {
                Object value = cell.getValue();
                if (value instanceof BigDecimal || value instanceof TemporalAccessor || value instanceof Date) {
                    value = value.toString();
                }
                if (value instanceof String && ((String) value).length() > 4096) {
                    throw new IOException("VALUE_LIMIT_EXCEEDED");
                }
                outputBudget -= cell.getKey().length() + (value == null ? 4 : value.toString().length()) + 32;
                if (outputBudget < 0) { throw new IOException("RESULT_LIMIT_EXCEEDED"); }
                row.put(cell.getKey(), value);
            }
            rows.add(row);
        }
        boolean more = skipped == offset && cursor.moveToNextRow();
        return map("table", name, "rows", rows, "next_offset", more ? offset + rows.size() : null,
            "atomic_snapshot", false);
    }
    public static Map<String, Object> layout(Database db, Path path) throws IOException {
        TableImpl table = (TableImpl) table(db, "PGPropiedades");
        List<Object> columns = new ArrayList<>();
        for (ColumnImpl column : table.getColumns()) {
            columns.add(map("name", column.getName(), "number", (int) column.getColumnNumber(),
                "type", column.getType().name(), "variable", column.isVariableLength(),
                "variable_index", column.getVarLenTableIndex()));
        }
        for (Index index : table.getIndexes()) {
            for (Index.Column key : index.getColumns()) {
                if ("Valor".equals(key.getName())) { throw new IOException("INDEXED_VALUE_UNSUPPORTED"); }
            }
        }
        DatabaseImpl impl = (DatabaseImpl) db;
        ByteBuffer decoded = impl.getPageChannel().createPageBuffer();
        impl.getPageChannel().readPage(decoded, table.getTableDefPageNumber());
        byte[] raw = new byte[4096];
        try (RandomAccessFile file = new RandomAccessFile(path.toFile(), "r")) {
            file.seek(4096L * table.getTableDefPageNumber()); file.readFully(raw);
        }
        if (!Arrays.equals(raw, decoded.array())) { throw new IOException("ENCODED_DATABASE_UNSUPPORTED"); }
        return map("table", table.getName(), "table_definition_page", table.getTableDefPageNumber(),
            "columns", columns, "value_indexed", false, "raw_pages", true);
    }
    public static void main(String[] args) {
        Map<String, Object> output;
        try {
            if (args.length < 2) { throw new IOException("INVALID_ARGUMENTS"); }
            Path path = Paths.get(args[1]);
            long size = Files.size(path), modified = Files.getLastModifiedTime(path).toMillis();
            try (Database db = new DatabaseBuilder(path.toFile()).setReadOnly(true).open()) {
                db.setDateTimeType(DateTimeType.LOCAL_DATE_TIME);
                db.setLinkResolver((source, name) -> { throw new IOException("LINKED_DATABASE_DISABLED"); });
                if ("describe".equals(args[0]) && args.length == 2) { output = describe(db); }
                else if ("read".equals(args[0]) && args.length == 6) {
                    output = read(db, args[2], args[3], Integer.parseInt(args[4]), Integer.parseInt(args[5]));
                } else if ("lab-layout".equals(args[0]) && args.length == 2) { output = layout(db, path); }
                else { throw new IOException("INVALID_ARGUMENTS"); }
            }
            if (size != Files.size(path) || modified != Files.getLastModifiedTime(path).toMillis()) {
                throw new IOException("SOURCE_CHANGED_DURING_READ");
            }
            output.put("status", "ok"); output.put("read_only", true);
        } catch (Exception ex) {
            output = map("status", "error", "code", "ACCESS_READ_FAILED");
        }
        byte[] json = (new GsonBuilder().serializeNulls().create().toJson(output) + "\n")
            .getBytes(StandardCharsets.UTF_8);
        System.out.write(json, 0, json.length);
        System.out.flush();
        if (!"ok".equals(output.get("status"))) { System.exit(1); }
    }
}
