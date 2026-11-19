const { createServer } = require("node:http");
const express = require("express");
const cors = require("cors");
const { postgraphile } = require("postgraphile");
const { LIST_TYPES, TYPES, listOfCodec } = require("postgraphile/@dataplan/pg");
const { makePgService } = require("postgraphile/adaptors/pg");
const { access, constant, defaultPlanResolver, lambda, SafeError } = require("postgraphile/grafast");
const { grafserv } = require("postgraphile/grafserv/express/v4");
const { PostGraphileAmberPreset } = require("postgraphile/presets/amber");
const { makeV4Preset } = require("postgraphile/presets/v4");
const { PgSimplifyInflectionPreset } = require("@graphile/simplify-inflection");
const { PostGraphileConnectionFilterPreset } = require("postgraphile-plugin-connection-filter");

const args = process.argv.slice(2);

if (args.length < 5) {
    console.error("Usage: postgraphile <connectionString> <schemas> <port> <page_limit> <timeout> <filterOnlyOnIndexedColumns> <disableAdvancedFilters> <skipNodeAliasPlugin>");
    process.exit(1);
}

const connectionString = args[0];
const schemas = args[1] === "" ? [] : args[1].split(",");
const port = Number(args[2]);
const pageLimit = Number(args[3]);
const statementTimeout = args[4];
const filterOnlyOnIndexedColumns = args[5] === "true";
const disableAdvancedFilters = args[6] === "true";
const skipNodeAliasPlugin = args[7] === "true";

function checkPageSizes([first, last]) {
    if (first > pageLimit || last > pageLimit) {
        throw new SafeError(`Pagination limit exceeded. Maximum allowed is ${pageLimit}.`);
    }
    return [last === undefined ? first || pageLimit : first, last];
}

const pageLimitPlugin = {
    name: "RindexerPageLimitPlugin",
    after: ["PgFirstLastBeforeAfterArgsPlugin"],
    schema: {
        hooks: {
            GraphQLObjectType_fields_field_args(args, _build, { scope }) {
                if (!args.first || !(scope.isPgFieldConnection || scope.isPgFieldSimpleCollection)) {
                    return args;
                }
                // Apply the two page sizes together so that a backward request can omit first.
                return {
                    ...args,
                    first: { ...args.first, applyPlan: undefined },
                    ...(args.last && { last: { ...args.last, applyPlan: undefined } }),
                };
            },
            GraphQLObjectType_fields_field(field, _build, { scope }) {
                if (!field.plan || !(scope.isPgFieldConnection || scope.isPgFieldSimpleCollection)) {
                    return field;
                }
                const plan = field.plan;
                return {
                    ...field,
                    plan($parent, args, info) {
                        const $collection = plan($parent, args, info);
                        if ($collection && args.$first) {
                            const $sizes = lambda([args.$first, args.$last ?? constant(undefined)], checkPageSizes);
                            $collection.addValidation?.(() => $sizes);
                            $collection.setFirst(access($sizes, [0]));
                            if (args.$last) {
                                $collection.setLast(access($sizes, [1]));
                            }
                        }
                        return $collection;
                    },
                };
            },
        },
    },
};

// Keep bytea as GraphQL String and retain PostgreSQL's hex strings inside arrays.
const byteaCodec = {
    ...TYPES.bytea,
    fromPg: (value) => value,
    toPg: (value) => value,
};
const byteaArrayCodec = listOfCodec(byteaCodec);

const byteaPlugin = {
    name: "RindexerByteaPlugin",
    after: ["PgCodecsPlugin"],
    gather: {
        hooks: {
            pgCodecs_findPgCodec(_info, event) {
                if (event.pgCodec === TYPES.bytea) {
                    event.pgCodec = byteaCodec;
                } else if (event.pgCodec === LIST_TYPES.bytea) {
                    event.pgCodec = byteaArrayCodec;
                }
            },
        },
    },
    schema: {
        hooks: {
            init: {
                before: ["PgCodecs"],
                callback(input, build) {
                    build.setGraphQLTypeForPgCodec(byteaCodec, ["input", "output"], "String");
                    return input;
                },
            },
            GraphQLObjectType_fields_field(field, _build, { scope }) {
                if (scope.pgFieldAttribute?.codec !== byteaCodec) {
                    return field;
                }
                const plan = field.plan ?? defaultPlanResolver;
                return {
                    ...field,
                    plan($parent, args, info) {
                        return lambda(plan($parent, args, info), (value) =>
                            value?.startsWith("\\x") ? `0x${value.slice(2)}` : value, true);
                    },
                };
            },
        },
    },
};

const preset = {
    extends: [
        PostGraphileAmberPreset,
        makeV4Preset({
            watchPg: true,
            disableDefaultMutations: true,
            dynamicJson: true,
            retryOnInitFail: true,
            sortExport: true,
            ignoreIndexes: !filterOnlyOnIndexedColumns,
            graphiql: false,
            simpleCollections: "omit",
            pgSettings: { statement_timeout: statementTimeout },
        }),
        PgSimplifyInflectionPreset,
        ...(disableAdvancedFilters ? [] : [PostGraphileConnectionFilterPreset]),
    ],
    pgServices: [makePgService({ connectionString, schemas })],
    plugins: [pageLimitPlugin, byteaPlugin],
    schema: {
        pgOmitListSuffix: false,
        pgSimplifyAllRows: false,
        pgShortPk: false,
        pgV4UseTableNameForNodeIdentifier: !skipNodeAliasPlugin,
        connectionFilterAllowedOperators: [
            "isNull",
            "equalTo",
            "notEqualTo",
            "distinctFrom",
            "notDistinctFrom",
            "lessThan",
            "lessThanOrEqualTo",
            "greaterThan",
            "greaterThanOrEqualTo",
            "in",
            "notIn",
        ],
    },
};

const htmlContent = `
    <div style="width: 100%; height: 100%;" id='embedded-sandbox'></div>
    <script src="https://embeddable-sandbox.cdn.apollographql.com/_latest/embeddable-sandbox.umd.production.min.js"></script>
    <script>
      new window.EmbeddedSandbox({
        target: '#embedded-sandbox',
        initialEndpoint: window.location.origin + '/graphql',
      });
    </script>
`;

async function main() {
    const app = express();
    app.use(cors());
    app.use(express.json());
    app.get("/playground", (_req, res) => {
        res.send(htmlContent);
    });

    const server = createServer(app);
    const pgl = postgraphile(preset);
    const serv = pgl.createServ(grafserv);
    await serv.addTo(app, server);
    server.listen(port, "0.0.0.0", () => {
        console.log(`GraphQL endpoint: http://localhost:${port}/graphql`);
        console.log(`GraphiQL Playground endpoint: http://localhost:${port}/playground`);
    });
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
