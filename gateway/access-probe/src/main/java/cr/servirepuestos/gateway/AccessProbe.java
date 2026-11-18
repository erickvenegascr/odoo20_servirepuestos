package cr.servirepuestos.gateway;

import com.google.gson.GsonBuilder;
import com.healthmarketscience.jackcess.Database;
import com.healthmarketscience.jackcess.DatabaseBuilder;
import com.healthmarketscience.jackcess.PropertyMap;
import com.healthmarketscience.jackcess.Row;
import com.healthmarketscience.jackcess.Table;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** Read-only identification, deliberately no update, exclusive lock or SQL API. */
public final class AccessProbe {
    private AccessProbe() { }

    static boolean relevant(String name) {
        if (name == null || name.length() > 120) { return false; }
        String text = Normalizer.normalize(name, Normalizer.Form.NFD)
            .replaceAll("\\p{M}", "").toLowerCase(Locale.ROOT);
        if (text.matches(".*(password|passwd|pwd|clave|token|secret|encript|encrypt).*")) { return false; }
        return text.matches(".*(articulo|artsservs|ultart|consecut|secuenc|proxnum|proximonum).*" );
    }

    static void add(List<Map<String, Object>> rows, String location, String name, Object value) {
        if (!relevant(name) || rows.size() >= 128) { return; }
        String text = value == null ? "" : String.valueOf(value);
        Object safe = value instanceof Boolean || text.matches("[+-]?[0-9]{1,18}") ? value : null;
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("location", location); row.put("name", name); row.put("value", safe);
        row.put("value_redacted", value != null && safe == null);
        rows.add(row);
    }

    private static void properties(List<Map<String, Object>> rows, String location, PropertyMap properties) {
        for (PropertyMap.Property property : properties) {
            if (relevant(property.getName())) { add(rows, location, property.getName(), property.getValue()); }
        }
    }

    public static Map<String, Object> inspect(Path path) throws IOException {
        if (!Files.isRegularFile(path) || !path.getFileName().toString().toLowerCase(Locale.ROOT).endsWith(".mdb")) {
            throw new IOException("INVALID_SOURCE");
        }
        long size = Files.size(path);
        long modified = Files.getLastModifiedTime(path).toMillis();
        List<Map<String, Object>> candidates = new ArrayList<>();
        List<String> warnings = new ArrayList<>();
        // Never create a database or resolve a linked MDB/ODBC source.
        try (Database database = new DatabaseBuilder(path.toFile()).setReadOnly(true).open()) {
            database.setLinkResolver((db, name) -> { throw new IOException("LINKED_DATABASE_DISABLED"); });
            properties(candidates, "database_properties", database.getDatabaseProperties());
            properties(candidates, "user_defined_properties", database.getUserDefinedProperties());
            properties(candidates, "summary_properties", database.getSummaryProperties());
            String tableName = null;
            for (String name : database.getTableNames()) {
                if ("PGPropiedades".equalsIgnoreCase(name)) { tableName = name; break; }
            }
            if (tableName != null) {
                try {
                    Table table = database.getTable(tableName);
                    for (Row row : table) {
                        Object name = row.get("Nombre");
                        if (name instanceof String) { add(candidates, "PGPropiedades", (String) name, row.get("Valor")); }
                    }
                } catch (Exception ex) { warnings.add("PROPERTIES_TABLE_UNAVAILABLE_OR_INCOMPLETE"); }
            } else { warnings.add("PROPERTIES_TABLE_NOT_FOUND"); }
        }
        boolean changed = size != Files.size(path) || modified != Files.getLastModifiedTime(path).toMillis();
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("status", changed ? "source_changed" : "inspected");
        result.put("source_name", path.getFileName().toString());
        result.put("read_only", true); result.put("business_writes", false);
        result.put("counter_confirmed", false); result.put("atomic_snapshot", false);
        result.put("source_changed_during_read", changed);
        result.put("candidates", candidates); result.put("warnings", warnings);
        result.put("limit_reached", candidates.size() >= 128);
        return result;
    }

    public static void main(String[] args) {
        Map<String, Object> result;
        if (args.length != 1) {
            result = new LinkedHashMap<>(); result.put("status", "error"); result.put("code", "ONE_MDB_PATH_REQUIRED");
        } else {
            try { result = inspect(Paths.get(args[0])); }
            catch (Exception ex) {
                // Native paths, internal records or connection data must not be printed.
                result = new LinkedHashMap<>(); result.put("status", "error"); result.put("code", "ACCESS_READ_FAILED");
            }
        }
        byte[] json = (new GsonBuilder().serializeNulls().create().toJson(result) + "\n")
            .getBytes(StandardCharsets.UTF_8);
        System.out.write(json, 0, json.length);
        System.out.flush();
        if (!"inspected".equals(result.get("status"))) { System.exit(1); }
    }
}
