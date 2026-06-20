# Project Consstitution 

***name*** Document Management


## Overview

This project is to create a Software Product. This product is a Document Management system that will allow complex specifications and collaboration materials to be stored in a text format that machines and robots can read, while also providing enough markup to produce rich documents that people can understand and render into PDF for delivery to 3rd parties that cannot access the repositories.  

Documents will be composed of parts (other documents included in an aggregate) that allow for sections, chapters, tables, and any partial section to be written in an independent document and included in the "main" document. 

Documents and partial documents will be represented as a complex graph, where a document can be referenced and pulled into any other document. While the base level structure will be a tree of folders and documents, documents should be free to reference any document in the entire structure.

Documents should be taggable for searching, and a full representation of a document that combines all partials should be able to be built at any time and stored as a numbered version. The system will keep track of numbered versions. Each numbered version will be available as both a text and a PDF. 

The application will use Git as a storage system for the documents produced. Saving involves committing changes with Git commit and pushing them to the repository. The system should continually poll the Git Repository for changes and merge them into the current repository to ensure everything is up to date. 

### Vision
1. A comprehensive document management system that allows large collections of working materials to be combined into complete documents that can be versioned and provided to 3rd parties.
2. A document management system that provides all text to be stored in a form that can be easily consumed and indexed by LLMs for reference and integration.
3. A system that is easy to navigate by document authors and researchers that allows for many authors to work at the same time within the document repository/library.
4. An artifact management system that allows for other materials, such as charts, graphs, and tables (CSV/Excel), to be pulled into documentation easily for inclusion as reference materials. Often, the same chart, graph, table, or graphic is needed in multiple documents. The ability to provide both the artifact and the description of the artifact in plain text.

### Objective 
1. Reduce the amount of time required to maintain specifications and complex working materials for projects by providing a repository that can reference all materials.
2. Create a rich ecosystem where both document materials and artifacts all reside along side each other and can be easily encorperated into each other.
3. A system that is both people-readable and maintainable but also allows for robots to work on documents, partials, and other artifacts in a way that multiple people can work together. 

### Scope
* Document Management
* Artifact Management

